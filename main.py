from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import anthropic, requests, os, traceback, uuid, re, unicodedata, hmac, hashlib, time
from dotenv import load_dotenv
from datetime import datetime, timedelta, date
import pytz
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

CLAUDE_KEY      = os.getenv("CLAUDE_KEY")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
# App Secret de tu app de Meta (Meta for Developers → tu app → Configuración básica →
# "Clave secreta de la app"). Se usa para verificar que los mensajes del webhook
# realmente vengan de WhatsApp/Meta y no de alguien que descubrió la URL.
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
PANEL_PASSWORD  = os.getenv("PANEL_PASSWORD", "ipiales2024")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
# Secreto para firmar los tokens de sesión de domiciliarios. Defínelo en Railway
# como variable de entorno propia (una cadena aleatoria larga) en vez de usar el fallback.
SESSION_SECRET  = os.getenv("SESSION_SECRET", PANEL_PASSWORD + "_dom_session_v1")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ADMIN_NUMBER  = "573167731698"
ZONA_HORARIA  = pytz.timezone("America/Bogota")

historial               = {}
mensajes_procesados     = set()

# ── RATE LIMITING ─────────────────────────────────────────────────────────────
# numero -> lista de timestamps de mensajes recientes
_rate_limit_timestamps  = {}
# numero -> timestamp hasta cuando está bloqueado
_rate_limit_bloqueados  = {}
RATE_LIMIT_MAX_MENSAJES = 10    # máximo mensajes por ventana
RATE_LIMIT_VENTANA_SEG  = 60    # ventana en segundos
RATE_LIMIT_BLOQUEO_SEG  = 60    # tiempo de bloqueo en segundos
MAX_CHARS_MENSAJE       = 500   # máximo caracteres por mensaje
clientes_esperando_decision = {}
clientes_esperando_calificacion = {}  # numero -> pedido_id
cliente_restaurante     = {}
clientes_eligiendo      = {}
# registro en pasos: numero -> {"paso": "nombre"|"direccion"}
clientes_registrando    = {}

INTERVALO_CORTO_MINUTOS = 15

# ── SUPABASE: RESTAURANTES Y MENÚ ────────────────────────────────────────────
# Cache en memoria para no consultar Supabase en cada mensaje
_cache_restaurantes = {}
_cache_menu         = {}   # restaurante_id -> [{"categoria":..,"descripcion":..,"activo":..}]

# Estado en memoria (domicilio forzado, espera, categorías desactivadas, notas).
# Se respalda en la tabla "restaurantes" de Supabase (ver _cargar_estado_extra_desde_db
# y guardar_estado_extra) para que sobreviva a un reinicio de Railway.
# defaultdict: cualquier restaurante nuevo (creado luego desde el panel admin)
# recibe automáticamente sus valores por defecto en lugar de lanzar KeyError.
from collections import defaultdict

def _estado_extra_default():
    return {
        "domicilio_activo": True,
        "tiempo_espera": None,
        "categorias_desactivadas": set(),
        "notas": [],
        "abierto_forzado": False,
        "fecha_forzado": None,
    }

_estado_extra = defaultdict(_estado_extra_default)

def cargar_restaurantes():
    global _cache_restaurantes
    rows = supabase.table("restaurantes").select("*").execute().data
    _cache_restaurantes = {r["id"]: r for r in rows}

def cargar_menu():
    global _cache_menu
    rows = supabase.table("menu_items").select("*").execute().data
    _cache_menu = {}
    for item in rows:
        rid = item["restaurante_id"]
        _cache_menu.setdefault(rid, []).append(item)

def _cargar_estado_extra_desde_db():
    """Reconstruye _estado_extra a partir de las columnas de la tabla restaurantes,
    para que la configuración diaria del admin sobreviva a un reinicio del servidor."""
    for key, r in _cache_restaurantes.items():
        extra = _estado_extra[key]
        extra["domicilio_activo"] = r.get("domicilio_activo", True)
        extra["tiempo_espera"] = r.get("tiempo_espera")
        extra["categorias_desactivadas"] = set(r.get("categorias_desactivadas") or [])
        extra["notas"] = list(r.get("notas") or [])
        extra["abierto_forzado"] = bool(r.get("abierto_forzado", False))
        fecha_str = r.get("fecha_forzado")
        extra["fecha_forzado"] = date.fromisoformat(fecha_str) if fecha_str else None

def guardar_estado_extra(rest_key):
    """Persiste el _estado_extra de un restaurante en Supabase (fire-and-forget)."""
    extra = _estado_extra[rest_key]
    try:
        supabase.table("restaurantes").update({
            "domicilio_activo": extra.get("domicilio_activo", True),
            "tiempo_espera": extra.get("tiempo_espera"),
            "categorias_desactivadas": list(extra.get("categorias_desactivadas", set())),
            "notas": extra.get("notas", []),
            "abierto_forzado": extra.get("abierto_forzado", False),
            "fecha_forzado": extra["fecha_forzado"].isoformat() if extra.get("fecha_forzado") else None,
        }).eq("id", rest_key).execute()
    except Exception:
        traceback.print_exc()

def get_restaurante(rest_key):
    return _cache_restaurantes.get(rest_key)

def get_menu(rest_key):
    return _cache_menu.get(rest_key, [])

# Carga inicial al arrancar
cargar_restaurantes()
cargar_menu()
_cargar_estado_extra_desde_db()

# ── CLIENTES SUPABASE ─────────────────────────────────────────────────────────

def get_cliente(numero):
    try:
        res = supabase.table("clientes").select("*").eq("numero", numero).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def crear_cliente(numero, nombre, direccion):
    try:
        # Bug 5 fix: verificar si ya existe antes de insertar (evita duplicados)
        existente = get_cliente(numero)
        if existente:
            # Ya existe, solo actualizar nombre y dirección si están vacíos
            actualizaciones = {}
            if not existente.get("nombre") and nombre:
                actualizaciones["nombre"] = nombre
            if not existente.get("direccion") and direccion:
                actualizaciones["direccion"] = direccion
            if actualizaciones:
                supabase.table("clientes").update(actualizaciones).eq("numero", numero).execute()
            return
        supabase.table("clientes").insert({
            "numero": numero,
            "nombre": nombre,
            "direccion": direccion,
        }).execute()
    except Exception:
        traceback.print_exc()

def actualizar_cliente(numero, datos):
    try:
        supabase.table("clientes").update(datos).eq("numero", numero).execute()
    except Exception:
        traceback.print_exc()

# ── PEDIDOS SUPABASE ──────────────────────────────────────────────────────────

def get_pedidos_activos(numero):
    try:
        res = supabase.table("pedidos").select("*")\
            .eq("numero_cliente", numero)\
            .in_("estado", ["activo", "preparando"])\
            .order("fecha", desc=True)\
            .execute()
        return res.data
    except Exception:
        return []

def get_pedido_by_id(pedido_id):
    try:
        res = supabase.table("pedidos").select("*").eq("id", pedido_id).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def get_todos_pedidos():
    try:
        res = supabase.table("pedidos").select("*").order("fecha", desc=True).limit(200).execute()
        return res.data or []
    except Exception:
        return []

def get_pedidos_restaurante(rest_key, desde=None, hasta=None, limite=200):
    """Pedidos de un solo restaurante, filtrados directo en la base de datos
    (no acotado por el límite global de get_todos_pedidos), con filtro opcional de fecha."""
    try:
        q = supabase.table("pedidos").select("*").eq("restaurante_id", rest_key).order("fecha", desc=True).limit(limite)
        if desde:
            q = q.gte("fecha", desde)
        if hasta:
            q = q.lte("fecha", hasta)
        res = q.execute()
        return res.data or []
    except Exception:
        traceback.print_exc()
        return []

def extraer_pedido_estructurado(conversacion_texto, rest_key):
    """Usa Claude para extraer el pedido en formato estructurado (JSON),
    eliminando la necesidad de adivinar tipo/dirección por texto libre."""
    r = get_restaurante(rest_key)
    prompt_extraccion = f"""Analiza esta conversación de un pedido en {r['nombre']} ({r['direccion']}) y responde SOLO con un JSON válido, sin texto adicional, sin markdown, sin explicación.

Formato exacto:
{{
  "tipo": "domicilio" o "recoger",
  "direccion": "dirección completa si es domicilio, o vacío si es recoger",
  "total": numero_entero_sin_puntos_ni_simbolos,
  "resumen_items": "lista de productos pedidos con cantidades y precios, en texto plano"
}}

Reglas:
- Si el cliente mencionó cualquier lugar de entrega (casa, edificio, barrio, calle, conjunto, punto de referencia), tipo es "domicilio".
- Si el cliente dijo explícitamente que recoge en el local, o nunca mencionó dirección y el bot preguntó y confirmó "recoger", tipo es "recoger".
- Si hay duda, prioriza "domicilio" si se mencionó algún lugar.
- total debe ser el monto final incluyendo domicilio si aplica.

Conversación:
{conversacion_texto}"""

    try:
        ai = anthropic.Anthropic(api_key=CLAUDE_KEY)
        resp = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt_extraccion}],
        )
        texto = resp.content[0].text.strip()
        # Limpiar posibles backticks de markdown
        texto = re.sub(r"^```json\s*|\s*```$", "", texto.strip(), flags=re.MULTILINE).strip()
        import json
        data = json.loads(texto)
        return data
    except Exception:
        traceback.print_exc()
        return None


def crear_pedido(numero, resumen, confirmacion_bot, rest_key, datos_estructurados=None):
    """Crea o actualiza el pedido. Si datos_estructurados viene de
    extraer_pedido_estructurado(), se usa eso (confiable). Si no,
    se cae al método anterior de adivinar por texto (respaldo)."""
    r = get_restaurante(rest_key)

    if datos_estructurados:
        tipo = datos_estructurados.get("tipo", "recoger")
        if tipo not in ["domicilio", "recoger"]:
            tipo = "recoger"
        direccion = datos_estructurados.get("direccion", "").strip()
        if tipo == "recoger" or not direccion:
            direccion = "En local" if tipo == "recoger" else "Ver resumen"
        try:
            total = int(datos_estructurados.get("total", 0))
        except (ValueError, TypeError):
            total = 0
        if not total:
            m = re.search(r"Total:?\s*\$?\s?([\d.,]+)", resumen, re.IGNORECASE)
            if m:
                total = int(m.group(1).replace(".", "").replace(",", ""))
    else:
        # Respaldo: método anterior por si falla la extracción estructurada
        es_domicilio = any(p in confirmacion_bot.lower() for p in ["camino", "domicilio a", "a la dirección"])
        tipo = "domicilio" if es_domicilio else "recoger"
        direccion = "En local"
        if es_domicilio:
            txt = confirmacion_bot.lower()
            for marca in ["domicilio a", "a la dirección"]:
                if marca in txt:
                    inicio = confirmacion_bot.lower().index(marca) + len(marca)
                    direccion = confirmacion_bot[inicio:].split(".")[0].strip()
                    break
            if direccion == "En local":
                direccion = "Ver resumen"
        total = 0
        m = re.search(r"Total:?\s*\$?\s?([\d.,]+)", resumen, re.IGNORECASE)
        if m:
            total = int(m.group(1).replace(".", "").replace(",", ""))

    ahora = datetime.now(ZONA_HORARIA)

    # ¿Ya tiene un pedido en estado "activo" (aún no aceptado)? Lo actualiza.
    # Si el pedido activo ya está "preparando" (ya aceptado por el restaurante/domiciliario),
    # NO se toca: se crea un pedido nuevo aparte para no perder ni retroceder el que ya está en curso.
    activos = get_pedidos_activos(numero)
    pedido_para_actualizar = next((p for p in activos if p["estado"] == "activo"), None)
    if pedido_para_actualizar:
        pedido_id = pedido_para_actualizar["id"]
        supabase.table("pedidos").update({
            "resumen": resumen,
            "total": total,
            "tipo": tipo,
            "direccion": direccion,
            "estado": "activo",
            "hora": ahora.strftime("%I:%M %p"),
            "fecha": ahora.isoformat(),
        }).eq("id", pedido_id).execute()
        return get_pedido_by_id(pedido_id), False

    pedido_id = str(uuid.uuid4())[:8].upper()
    pedido = {
        "id": pedido_id,
        "numero_cliente": numero,
        "restaurante_id": rest_key,
        "restaurante_nombre": r["nombre"] if r else rest_key,
        "resumen": resumen,
        "total": total,
        "tipo": tipo,
        "direccion": direccion,
        "estado": "activo",
        "modificaciones": [],
        "quejas": [],
        "hora": ahora.strftime("%I:%M %p"),
        "fecha": ahora.isoformat(),
    }
    supabase.table("pedidos").insert(pedido).execute()
    return pedido, True

def actualizar_estado_pedido(pedido_id, nuevo_estado):
    supabase.table("pedidos").update({"estado": nuevo_estado}).eq("id", pedido_id).execute()

def agregar_modificacion(pedido_id, texto):
    pedido = get_pedido_by_id(pedido_id)
    if pedido:
        mods = pedido.get("modificaciones") or []
        mods.append(texto)
        supabase.table("pedidos").update({"modificaciones": mods}).eq("id", pedido_id).execute()

def agregar_queja(pedido_id, texto):
    pedido = get_pedido_by_id(pedido_id)
    if pedido:
        quejas = pedido.get("quejas") or []
        quejas.append(texto)
        supabase.table("pedidos").update({"quejas": quejas}).eq("id", pedido_id).execute()

def guardar_calificacion(pedido_id, calificacion, comentario):
    try:
        datos = {"calificacion": calificacion}
        if comentario:
            datos["comentario_calificacion"] = comentario
        supabase.table("pedidos").update(datos).eq("id", pedido_id).execute()
    except Exception:
        traceback.print_exc()

def buscar_pedido_activo_cliente(numero):
    activos = get_pedidos_activos(numero)
    if not activos:
        return None
    p = activos[0]
    try:
        fecha_str = p["fecha"]
        # Supabase devuelve timestamps en UTC con +00:00 o Z
        # Convertimos correctamente a Colombia para comparar
        if fecha_str.endswith("Z"):
            fecha_str = fecha_str.replace("Z", "+00:00")
        hora_pedido = datetime.fromisoformat(fecha_str)
        # Si no tiene zona horaria, asumimos UTC
        if hora_pedido.tzinfo is None:
            import pytz as _pytz
            hora_pedido = _pytz.utc.localize(hora_pedido)
        # Convertir a Colombia
        hora_pedido_col = hora_pedido.astimezone(ZONA_HORARIA)
        ahora_col = datetime.now(ZONA_HORARIA)
        if (ahora_col - hora_pedido_col) <= timedelta(minutes=INTERVALO_CORTO_MINUTOS):
            return p
        # Si es más viejo de 15 min, solo retorna si sigue activo/preparando
        # (para que el cliente no quede bloqueado indefinidamente)
        return p
    except Exception:
        traceback.print_exc()
        return p

# ── HELPERS RESTAURANTES ──────────────────────────────────────────────────────

def normalizar_texto(s):
    """Quita tildes y pasa a minúsculas para comparar nombres sin depender de acentos."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()

DIAS_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
DIAS_SEMANA_ABREV = {"lunes": "Lun", "martes": "Mar", "miercoles": "Mié", "jueves": "Jue", "viernes": "Vie", "sabado": "Sáb", "domingo": "Dom"}

def esta_abierto(rest_key):
    r = get_restaurante(rest_key)
    if not r:
        return False
    extra = _estado_extra.get(rest_key, {})
    ahora = datetime.now(ZONA_HORARIA)
    if extra.get("abierto_forzado") and extra.get("fecha_forzado") == ahora.date():
        return True
    if extra.get("abierto_forzado") and extra.get("fecha_forzado") != ahora.date():
        extra["abierto_forzado"] = False
        extra["fecha_forzado"] = None
        guardar_estado_extra(rest_key)
    if not r.get("activo", True):
        return False
    horario = r.get("horario_semanal")
    if horario:
        dia = DIAS_SEMANA[ahora.weekday()]
        cfg = horario.get(dia) or {}
        if not cfg.get("abierto", False):
            return False
        return cfg.get("hora_inicio", 0) <= ahora.hour < cfg.get("hora_fin", 0)
    return r["hora_inicio"] <= ahora.hour < r["hora_fin"]

def formato_horario(r):
    """Texto legible del horario: por día si hay horario_semanal configurado,
    o la franja fija de siempre si no."""
    horario = r.get("horario_semanal")
    if not horario:
        return f"{r['hora_inicio']}:00 – {r['hora_fin']}:00 (todos los días)"
    partes = []
    for dia in DIAS_SEMANA:
        cfg = horario.get(dia) or {}
        abrev = DIAS_SEMANA_ABREV[dia]
        if cfg.get("abierto"):
            partes.append(f"{abrev} {cfg.get('hora_inicio', 0)}:00-{cfg.get('hora_fin', 0)}:00")
        else:
            partes.append(f"{abrev} Cerrado")
    return ", ".join(partes)

def lista_restaurantes():
    lineas = ["🍽️ *Bienvenido a Ipiales Delivery*\n\nElige un restaurante:\n"]
    for i, (key, r) in enumerate(_cache_restaurantes.items(), 1):
        estado = "✅ Abierto" if esta_abierto(key) else "❌ Cerrado"
        lineas.append(f"{i}. *{r['nombre']}* — {estado}\n   📍 {r['direccion']}")
    lineas.append("\nResponde con el *número* o el *nombre* del restaurante.")
    return "\n".join(lineas)

def build_system_prompt(rest_key, cliente=None):
    r = get_restaurante(rest_key)
    extra = _estado_extra.get(rest_key, {})
    items = get_menu(rest_key)
    desact = extra.get("categorias_desactivadas", set())
    menu_activo_items = [i for i in items if i["activo"] and i["categoria"] not in desact]
    menu_activo = [i["descripcion"] for i in menu_activo_items]
    hay_bebidas = any("bebida" in normalizar_texto(i["categoria"]) for i in menu_activo_items)
    notas = ("\nNOTAS DE HOY:\n- " + "\n- ".join(extra["notas"])) if extra.get("notas") else ""
    espera = f"\nTIEMPO DE ESPERA: {extra['tiempo_espera']} minutos." if extra.get("tiempo_espera") else ""
    dom = "Sí. Costo: $3.000." if extra.get("domicilio_activo", True) else "No disponible."

    saludo = ""
    if cliente:
        saludo = f"\nEl cliente se llama *{cliente['nombre']}* y su dirección habitual es *{cliente['direccion']}*. Salúdalo por su nombre."

    upsell = (
        '\n- Si al momento de cerrar el pedido el cliente no ha pedido ninguna bebida, sugiérele UNA sola vez '
        'agregar algo de tomar (ej: "¿quieres agregar algo de tomar? 🥤") antes de mostrar el resumen final. '
        'Si dice que no, respeta su decisión y no insistas de nuevo con eso.'
    ) if hay_bebidas else ""

    return f"""Eres el asistente virtual de *{r['nombre']}*, en {r['direccion']}, Ipiales.
HORARIO: {formato_horario(r)}
DOMICILIO: {dom}
MÉTODOS DE PAGO: Nequi, Daviplata, transferencia, efectivo.
MENÚ:
{chr(10).join(menu_activo)}
{notas}{espera}{saludo}

INSTRUCCIONES:
- Habla amigable y natural como empleado real de {r['nombre']}.
- Si el cliente tiene dirección guardada y pide domicilio, úsala directamente sin preguntar de nuevo.
- Acumula todos los productos sin mostrar resumen parcial.
- Si el cliente pide algo ambiguo (falta tamaño, sabor, o hay varias opciones parecidas en el menú), pregunta cuál quiere exactamente antes de agregarlo al pedido — no asumas ni adivines.
- Confirma la cantidad exacta de cada producto a medida que el cliente lo va pidiendo.
- NUNCA agregues al pedido un producto que no esté escrito tal cual en el MENÚ de arriba.
- NUNCA muestres resumen ni total hasta que el cliente diga "es todo", "listo", "eso sería" o similar.{upsell}
- Solo entonces muestra resumen completo con total.
- Si el cliente mencionó lugar de entrega, es domicilio. Confirma la dirección.
- Al confirmar el pedido SIEMPRE termina con esta frase EXACTA en una línea separada: "✅ Pedido recibido. Estamos preparando tu pedido 🍔"
- NUNCA uses frases como "en camino", "listo para recoger", "pasamos a preparar" — eso lo decide el restaurante, no tú.
- Esta frase de confirmación es OBLIGATORIA cada vez que el cliente confirme un pedido.
- No inventes productos ni precios.
- Si el cliente pregunta por otros restaurantes, quiere cambiar de restaurante, o pide ver la lista de restaurantes, responde EXACTAMENTE: "Claro 😊 Escribe *restaurantes* para ver todos los restaurantes disponibles."
- NO digas que solo eres asistente de este restaurante. El cliente puede cambiar cuando quiera.
- Responde siempre en español. Sé conciso."""

# ── ENVÍO WHATSAPP ────────────────────────────────────────────────────────────

def enviar_whatsapp(numero, mensaje):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": mensaje}}
    r = requests.post(url, headers=headers, json=data)
    print("META →", r.status_code, r.text)
    return r

def enviar_whatsapp_documento(numero, link, filename, caption):
    """Manda un archivo (ej. el PDF del menú de un restaurante) como documento
    adjunto de WhatsApp, en vez de solo un link de texto."""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "document",
        "document": {"link": link, "filename": filename, "caption": caption},
    }
    r = requests.post(url, headers=headers, json=data)
    print("META (documento) →", r.status_code, r.text)
    return r

def enviar_menu_texto(numero, rest_key):
    r = get_restaurante(rest_key)
    extra = _estado_extra.get(rest_key, {})
    items = get_menu(rest_key)
    desact = extra.get("categorias_desactivadas", set())
    lineas = [f"📋 *Menú de {r['nombre']}*\n"]
    for item in items:
        if item["activo"] and item["categoria"] not in desact:
            lineas.append(item["descripcion"])
    if extra.get("notas"):
        lineas.append("\n📝 *Notas de hoy:*")
        for nota in extra["notas"]:
            lineas.append(f"- {nota}")
    dom = "Sí, costo $3.000" if extra.get("domicilio_activo", True) else "No disponible"
    lineas.append(f"\n🛵 *Domicilio:* {dom}")
    lineas.append("💳 *Pago:* Nequi, Daviplata, transferencia, efectivo")
    enviar_whatsapp(numero, "\n\n".join(lineas))

# ── NOTIFICACIONES ADMIN ──────────────────────────────────────────────────────

def notificar_pedido_admin(numero, pedido, es_nuevo=True):
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    prefijo = "🛎️ *Pedido nuevo*" if es_nuevo else "🔄 *Pedido actualizado*"
    msg = (
        f"{prefijo} #{pedido['id']}\n"
        f"🍽️ {pedido.get('restaurante_nombre', '')}\n"
        f"📱 +{numero}\n"
        f"🕐 {pedido['hora']}\n"
        f"{icono} {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger'}\n"
        f"📍 {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}\n"
        f"────────────────\n"
        f"👉 {os.getenv('PANEL_URL', '')}/panel"
    )
    enviar_whatsapp(ADMIN_NUMBER, msg)

def notificar_pedido_restaurante(pedido, rest_key, es_nuevo=True):
    """Manda el aviso de pedido nuevo/actualizado directo al WhatsApp del
    restaurante, si tiene un número configurado para eso."""
    r = get_restaurante(rest_key)
    numero_rest = r.get("whatsapp_notificacion") if r else None
    if not numero_rest:
        return
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    prefijo = "🛎️ *Pedido nuevo*" if es_nuevo else "🔄 *Pedido actualizado*"
    msg = (
        f"{prefijo} #{pedido['id']}\n"
        f"🕐 {pedido['hora']}\n"
        f"{icono} {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger'}\n"
        f"📍 {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}\n"
        f"────────────────\n"
        f"👉 Entra a tu panel: {os.getenv('PANEL_URL', '')}/panel-restaurante"
    )
    enviar_whatsapp(numero_rest, msg)

# ── DOMICILIARIOS ────────────────────────────────────────────────────────────

def get_domiciliarios_disponibles():
    try:
        res = supabase.table("domiciliarios").select("*").eq("activo", True).eq("disponible", True).execute()
        return res.data or []
    except Exception:
        traceback.print_exc()
        return []

def get_domiciliario_by_nombre(nombre):
    try:
        res = supabase.table("domiciliarios").select("*").eq("nombre", nombre).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def get_domiciliario_by_telefono(telefono):
    try:
        res = supabase.table("domiciliarios").select("*").eq("telefono", telefono).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def get_domiciliario_by_id(dom_id):
    try:
        res = supabase.table("domiciliarios").select("*").eq("id", dom_id).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def get_nombres_domiciliarios_activos():
    try:
        res = supabase.table("domiciliarios").select("nombre").eq("activo", True).order("nombre").execute()
        return [d["nombre"] for d in (res.data or [])]
    except Exception:
        traceback.print_exc()
        return []

# ── SESIÓN DOMICILIARIOS ──────────────────────────────────────────────────────
# Token firmado (HMAC) en vez de confiar ciegamente en el nombre que manda el cliente.
# Así, cada acción (aceptar/entregar/etc) exige haber iniciado sesión con el PIN correcto.

def generar_token_dom(dom_id):
    return hmac.new(SESSION_SECRET.encode(), str(dom_id).encode(), hashlib.sha256).hexdigest()

def verificar_sesion_dom(dom_id, token):
    if not dom_id or not token:
        return None
    if not hmac.compare_digest(generar_token_dom(dom_id), str(token)):
        return None
    dom = get_domiciliario_by_id(dom_id)
    if not dom or not dom.get("activo", True):
        return None
    return dom

# ── SEGURIDAD DEL WEBHOOK ─────────────────────────────────────────────────────

def verificar_firma_webhook(raw_body: bytes, firma_header: str) -> bool:
    """Verifica que el payload del webhook venga realmente de Meta/WhatsApp
    (HMAC-SHA256 del cuerpo crudo con el App Secret), no de alguien que
    fabricó la petición a mano conociendo la URL.
    Si WHATSAPP_APP_SECRET no está configurada, se deja pasar (para no tumbar
    el bot por accidente) pero se advierte en los logs — configúrala en Railway."""
    if not WHATSAPP_APP_SECRET:
        print("⚠️ WHATSAPP_APP_SECRET no está configurada: el webhook NO está protegido contra mensajes falsificados.")
        return True
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    firma_recibida = firma_header.split("=", 1)[1]
    return hmac.compare_digest(firma_esperada, firma_recibida)

def set_disponible_domiciliario(telefono, disponible):
    try:
        supabase.table("domiciliarios").update({"disponible": disponible}).eq("telefono", telefono).execute()
    except Exception:
        traceback.print_exc()

def verificar_rate_limit(numero):
    """Verifica si el número puede enviar mensajes.
    Retorna (permitido, mensaje_error)"""
    ahora = datetime.now(ZONA_HORARIA).timestamp()

    # ¿Está bloqueado temporalmente?
    if numero in _rate_limit_bloqueados:
        hasta = _rate_limit_bloqueados[numero]
        if ahora < hasta:
            segundos_restantes = int(hasta - ahora)
            return False, (
                f"⏳ Demasiados mensajes seguidos. "
                f"Por favor espera {segundos_restantes} segundos antes de escribir de nuevo."
            )
        else:
            del _rate_limit_bloqueados[numero]

    # Limpiar timestamps viejos
    if numero not in _rate_limit_timestamps:
        _rate_limit_timestamps[numero] = []
    _rate_limit_timestamps[numero] = [
        t for t in _rate_limit_timestamps[numero]
        if ahora - t < RATE_LIMIT_VENTANA_SEG
    ]

    # Verificar límite
    if len(_rate_limit_timestamps[numero]) >= RATE_LIMIT_MAX_MENSAJES:
        _rate_limit_bloqueados[numero] = ahora + RATE_LIMIT_BLOQUEO_SEG
        _rate_limit_timestamps[numero] = []
        return False, (
            f"⏳ Enviaste demasiados mensajes seguidos. "
            f"Por favor espera {RATE_LIMIT_BLOQUEO_SEG} segundos."
        )

    # Registrar este mensaje
    _rate_limit_timestamps[numero].append(ahora)

    # Limpiar memoria si hay muchos números (máx 1000)
    if len(_rate_limit_timestamps) > 1000:
        _rate_limit_timestamps.clear()

    return True, None


def procesar_mensaje_domiciliario(numero, texto):
    """Maneja mensajes de domiciliarios (entro turno / salgo turno).
    Retorna respuesta si el número es de un domiciliario, None si no."""
    dom = get_domiciliario_by_telefono(numero)
    if not dom:
        return None

    t = texto.strip().lower()

    if any(p in t for p in ["entro turno", "entro al turno", "inicio turno", "empiezo turno", "estoy disponible", "ya estoy"]):
        set_disponible_domiciliario(numero, True)
        return (
            f"✅ *¡Listo {dom['nombre']}!* Estás en turno.\n\n"
            f"Te avisaremos cuando haya un pedido disponible. 🛵\n\n"
            f"Escribe *salgo turno* cuando termines."
        )

    if any(p in t for p in ["salgo turno", "salgo del turno", "termino turno", "fin turno", "ya no estoy", "me voy"]):
        set_disponible_domiciliario(numero, False)
        return f"👋 *Hasta luego {dom['nombre']}!* Quedas fuera de turno.\n\nEscribe *entro turno* cuando vuelvas a estar disponible."

    if any(p in t for p in ["ayuda", "help", "comandos"]):
        return (
            f"🛵 *Comandos disponibles {dom['nombre']}:*\n\n"
            f"• *entro turno* → activarte para recibir pedidos\n"
            f"• *salgo turno* → desactivarte\n\n"
            f"Estado actual: {'✅ En turno' if dom.get('disponible') else '❌ Fuera de turno'}"
        )

    # Si es domiciliario pero no es un comando conocido,
    # responde brevemente sin pasar por Claude
    if dom.get("disponible"):
        return f"Hola {dom['nombre']} 😊 Estás en turno. Escribe *salgo turno* si quieres desactivarte."
    else:
        return f"Hola {dom['nombre']} 😊 Estás fuera de turno. Escribe *entro turno* para activarte."

def pedido_ya_asignado(pedido_id):
    try:
        res = supabase.table("asignaciones").select("*").eq("pedido_id", pedido_id).execute()
        return len(res.data) > 0
    except Exception:
        return False

# Pedidos pendientes de asignacion (en memoria, se limpia al asignar)
_pedidos_pendientes = {}  # pedido_id -> pedido dict

def agregar_pedido_pendiente(pedido):
    _pedidos_pendientes[pedido["id"]] = pedido

def notificar_domiciliarios_whatsapp(pedido):
    """Notifica a domiciliarios disponibles por WhatsApp como respaldo"""
    doms = get_domiciliarios_disponibles()
    for dom in doms:
        msg = (
            f"🛵 *¡Pedido nuevo #{pedido['id']}!*\n"
            f"🍽️ {pedido.get('restaurante_nombre', '')}\n"
            f"📍 {pedido.get('direccion', '')}\n"
            f"────────────────\n"
            f"{pedido.get('resumen', '')}\n"
            f"────────────────\n"
            f"💰 Ganancia: $5.000\n\n"
            f"👉 Abre la app para aceptar:\n"
            f"{os.getenv('PANEL_URL', '')}/domiciliarios"
        )
        enviar_whatsapp(dom["telefono"], msg)

# ── COMANDOS ADMIN ────────────────────────────────────────────────────────────

def procesar_comando_admin(texto):
    t = texto.strip().lower()

    if t in ["ayuda", "help", "comandos"]:
        return (
            "🛠️ *Comandos admin:*\n\n"
            "*Restaurantes:*\n"
            "• abre las bravas → abre solo hoy\n"
            "• cierra escarabajo\n"
            "• abre monaco\n\n"
            "*Por restaurante:*\n"
            "• espera 30 las bravas\n"
            "• sin espera escarabajo\n"
            "• quita hamburguesas escarabajo\n"
            "• activa pizzas monaco\n"
            "• nota no hay pepperoni monaco\n"
            "• borra notas las bravas\n"
            "• quita domicilio monaco\n"
            "• activa domicilio las bravas\n\n"
            "*General:*\n"
            "• estado → ver todo\n"
            "• recargar menu → recarga menú desde BD"
        )

    if t in ["estado", "ver estado"]:
        lineas = ["📋 *Estado restaurantes:*\n"]
        for key, r in _cache_restaurantes.items():
            extra = _estado_extra.get(key, {})
            abierto = "✅ Abierto" if esta_abierto(key) else "❌ Cerrado"
            forzado = " *(forzado)*" if extra.get("abierto_forzado") else ""
            dom = "✅" if extra.get("domicilio_activo", True) else "❌"
            espera = f"{extra.get('tiempo_espera')}min" if extra.get("tiempo_espera") else "—"
            desact = ", ".join(extra.get("categorias_desactivadas", set())) or "ninguna"
            lineas.append(
                f"*{r['nombre']}*\n"
                f"  {abierto}{forzado} | 🛵 {dom} | ⏱ {espera}\n"
                f"  ❌ Desact: {desact}"
            )
        return "\n\n".join(lineas)

    if t in ["recargar menu", "recargar menú"]:
        cargar_restaurantes()
        cargar_menu()
        _cargar_estado_extra_desde_db()
        return "✅ Menú y restaurantes recargados desde la base de datos."

    # Identificar restaurante
    rest_key = None
    t_normalizado = normalizar_texto(t)
    for key, r in _cache_restaurantes.items():
        if normalizar_texto(r["nombre"]) in t_normalizado or key.replace("_", " ") in t or key in t:
            rest_key = key
            break

    if t.startswith("abre ") or t.startswith("abrir "):
        if rest_key:
            _estado_extra[rest_key]["abierto_forzado"] = True
            _estado_extra[rest_key]["fecha_forzado"] = datetime.now(ZONA_HORARIA).date()
            guardar_estado_extra(rest_key)
            return f"✅ *{_cache_restaurantes[rest_key]['nombre']}* abierto hoy. Mañana se cierra solo."
        return "⚠️ No encontré el restaurante."

    if t.startswith("cierra ") or t.startswith("cerrar "):
        if rest_key:
            _estado_extra[rest_key]["abierto_forzado"] = False
            _estado_extra[rest_key]["fecha_forzado"] = None
            supabase.table("restaurantes").update({"activo": False}).eq("id", rest_key).execute()
            cargar_restaurantes()
            guardar_estado_extra(rest_key)
            return f"✅ *{_cache_restaurantes[rest_key]['nombre']}* cerrado."
        return "⚠️ No encontré el restaurante."

    if rest_key is None:
        return None

    extra = _estado_extra[rest_key]
    nombre = _cache_restaurantes[rest_key]["nombre"]

    if "quita domicilio" in t or "desactiva domicilio" in t:
        extra["domicilio_activo"] = False
        guardar_estado_extra(rest_key)
        return f"✅ Domicilio desactivado en *{nombre}*."
    if "activa domicilio" in t:
        extra["domicilio_activo"] = True
        guardar_estado_extra(rest_key)
        return f"✅ Domicilio activado en *{nombre}*."

    m = re.search(r"espera\s+(\d+)", t)
    if m:
        extra["tiempo_espera"] = int(m.group(1))
        guardar_estado_extra(rest_key)
        return f"✅ Espera de *{m.group(1)} min* en {nombre}."
    if "sin espera" in t or "quita espera" in t:
        extra["tiempo_espera"] = None
        guardar_estado_extra(rest_key)
        return f"✅ Espera eliminada en {nombre}."

    if "borra notas" in t or "quita notas" in t:
        extra["notas"].clear()
        guardar_estado_extra(rest_key)
        return f"✅ Notas borradas en *{nombre}*."
    if t.startswith("nota "):
        nota = re.sub(r"nota\s+", "", t).replace(_cache_restaurantes[rest_key]["nombre"].lower(), "").replace(rest_key.replace("_", " "), "").strip()
        extra["notas"].append(nota)
        guardar_estado_extra(rest_key)
        return f"✅ Nota en *{nombre}*: '{nota}'"

    items = get_menu(rest_key)
    categorias = list({i["categoria"] for i in items})

    if t.startswith("quita ") or t.startswith("desactiva "):
        palabra = re.sub(r"(quita|desactiva)\s+", "", t).replace(_cache_restaurantes[rest_key]["nombre"].lower(), "").replace(rest_key.replace("_", " "), "").strip()
        for cat in categorias:
            if palabra in cat or cat in palabra:
                extra["categorias_desactivadas"].add(cat)
                guardar_estado_extra(rest_key)
                return f"✅ *{cat}* desactivado en {nombre}."
        return f"⚠️ No encontré '{palabra}'. Categorías: {', '.join(categorias)}"

    if t.startswith("activa ") or t.startswith("pon "):
        palabra = re.sub(r"(activa|pon)\s+", "", t).replace(_cache_restaurantes[rest_key]["nombre"].lower(), "").replace(rest_key.replace("_", " "), "").strip()
        for cat in categorias:
            if palabra in cat or cat in palabra:
                extra["categorias_desactivadas"].discard(cat)
                guardar_estado_extra(rest_key)
                return f"✅ *{cat}* activado en {nombre}."
        return f"⚠️ No encontré '{palabra}'."

    return None

# ── PANEL WEB ─────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ipiales Delivery — Login</title>
<style>
*{box-sizing:border-box}body{background:#FFF8E7;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#222}
.box{background:#fff;border:1px solid #FFE2A8;box-shadow:0 8px 28px rgba(0,0,0,.08);padding:32px 28px;border-radius:16px;text-align:center;width:90%;max-width:340px}
h1{font-size:1.3rem;margin-bottom:4px}h1 span{color:#F57C00}p{color:#9a8a6b;margin-bottom:20px;font-size:.87rem}
input{width:100%;padding:12px;background:#FFFBF2;border:1px solid #FFE2A8;border-radius:10px;font-size:1rem;outline:none;margin-bottom:12px}
input:focus{border-color:#FFC107;box-shadow:0 0 0 3px rgba(255,193,7,.18)}
button{width:100%;padding:12px;background:linear-gradient(135deg,#FFC107,#F57C00);border:none;border-radius:10px;color:#1a1a1a;font-weight:700;font-size:1rem;cursor:pointer}
</style></head><body>
<div class="box">
  <h1>🍽️ IPIALES <span>DELIVERY</span></h1>
  <p>Panel de pedidos</p>
  <form onsubmit="entrar(event)">
    <input type="password" id="pw" placeholder="Contraseña" autofocus>
    <button type="submit">Entrar</button>
  </form>
</div>
<script>function entrar(e){e.preventDefault();window.location.href='/panel?pw='+encodeURIComponent(document.getElementById('pw').value);}</script>
</body></html>"""

LOGIN_RESTAURANTE_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel del restaurante — Ipiales Delivery</title>
<style>
*{box-sizing:border-box}body{background:#FFF8E7;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#222}
.box{background:#fff;border:1px solid #FFE2A8;box-shadow:0 8px 28px rgba(0,0,0,.08);padding:32px 28px;border-radius:16px;text-align:center;width:90%;max-width:340px}
h1{font-size:1.3rem;margin-bottom:4px}h1 span{color:#F57C00}p{color:#9a8a6b;margin-bottom:20px;font-size:.87rem}
input{width:100%;padding:12px;background:#FFFBF2;border:1px solid #FFE2A8;border-radius:10px;font-size:1rem;outline:none;margin-bottom:12px}
input:focus{border-color:#FFC107;box-shadow:0 0 0 3px rgba(255,193,7,.18)}
button{width:100%;padding:12px;background:linear-gradient(135deg,#FFC107,#F57C00);border:none;border-radius:10px;color:#1a1a1a;font-weight:700;font-size:1rem;cursor:pointer}
</style></head><body>
<div class="box">
  <h1>🍽️ Panel de <span>tu restaurante</span></h1>
  <p>Ipiales Delivery</p>
  <form id="form-login">
    <input type="password" id="pw" placeholder="Contraseña de tu restaurante" autofocus>
    <div class="err" id="err" style="color:#c0392b;font-size:.82rem;margin:-6px 0 12px;display:none">Contraseña incorrecta</div>
    <button type="submit">Entrar</button>
  </form>
</div>
<script>
document.getElementById('form-login').onsubmit = async function(e) {
  e.preventDefault();
  const err = document.getElementById('err');
  err.style.display = 'none';
  try {
    const r = await fetch('/panel-restaurante/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: document.getElementById('pw').value})});
    const d = await r.json();
    if (d.ok) { window.location.href = '/panel-restaurante'; }
    else { err.textContent = d.msg || 'Contraseña incorrecta'; err.style.display = 'block'; }
  } catch (e) { err.textContent = 'Error de conexión'; err.style.display = 'block'; }
};
</script>
</body></html>"""

PANEL_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel — Ipiales Delivery</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:#f5f5f0;color:#222;padding:16px;min-height:100vh}
.container{max-width:1200px;margin:0 auto}
header{background:linear-gradient(135deg,#FFC107,#F57C00);border-radius:14px;padding:16px 20px;margin-bottom:18px;box-shadow:0 4px 16px rgba(245,124,0,.25)}
h1{font-size:1.3rem;color:#222}h1 span{display:block;font-size:.75rem;font-weight:400;color:rgba(34,32,24,.7);margin-top:2px}
.header-top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
.btns{display:flex;gap:8px}
.btn{padding:9px 16px;border:none;border-radius:8px;cursor:pointer;font-weight:700;font-size:.83rem}
.btn-dark{background:#222;color:#fff}.btn-dark:hover{background:#333}
.btn-out{background:rgba(34,32,24,.8);color:#fff}.btn-out:hover{background:#222}
.stats{display:flex;gap:16px;margin-top:10px;font-size:.8rem;color:rgba(34,32,24,.7);font-weight:600;flex-wrap:wrap}
.cards-resumen{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.card-r{background:#fff;border-radius:10px;padding:12px 14px;text-align:center;border:1px solid #FFE2A8}
.card-r .num{display:block;font-size:1.5rem;font-weight:700;color:#222}
.card-r .lbl{display:block;font-size:.72rem;color:#888;margin-top:3px}
.card-r.total{background:#222;border-color:#222}.card-r.total .num{color:#FFC107;font-size:1.7rem}.card-r.total .lbl{color:rgba(255,255,255,.6)}
.card-r.cancel .num{color:#d32f2f}
.tabs{display:flex;gap:8px;margin:14px 0;flex-wrap:wrap}
.tab{background:#fff;color:#888;border:1px solid #FFE2A8;padding:8px 14px;border-radius:20px;cursor:pointer;font-size:.83rem;font-weight:700;transition:all .15s}
.tab:hover{border-color:#FFA000;color:#222}
.tab.on{background:linear-gradient(135deg,#FFC107,#F57C00);color:#222;border-color:#F57C00}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{background:#fff;border-left:4px solid #F57C00;border-radius:10px;padding:14px;box-shadow:0 2px 8px rgba(0,0,0,.07)}
.pid{font-size:1.1rem;font-weight:700;color:#F57C00}
.rest-tag{display:inline-block;background:#FFF3D6;color:#E65100;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:700;margin:4px 0 2px}
.hora{font-size:.78rem;color:#888}
.tipo-tag{display:inline-block;background:#FFC107;color:#222;padding:3px 8px;border-radius:4px;font-size:.73rem;font-weight:700;margin-top:6px}
.cli{font-size:.83rem;color:#555;margin:6px 0 2px}
.cli-nombre{font-size:.83rem;color:#E65100;font-weight:700;margin:2px 0}
.dir{font-size:.78rem;color:#888;margin-bottom:6px;word-break:break-word}
.resumen{background:#FFFBF2;padding:8px;border-radius:6px;font-size:.76rem;color:#444;max-height:100px;overflow-y:auto;border-left:3px solid #FFC107;white-space:pre-wrap;margin:6px 0}
.mods{background:#FFF6E0;border-left:3px solid #FF9800;padding:7px;border-radius:6px;margin:6px 0;font-size:.73rem}
.quejas-box{background:#FDECEA;border-left:3px solid #f44336;padding:7px;border-radius:6px;margin:6px 0;font-size:.73rem}
.est-lbl{text-align:center;font-size:.78rem;font-weight:700;padding:4px;border-radius:4px;margin:8px 0 5px}
.est-lbl.activo{color:#2E7D32}.est-lbl.preparando{color:#1565C0}.est-lbl.enviado{color:#1565C0}.est-lbl.entregado{color:#6A1B9A}.est-lbl.cancelado{color:#C62828}
.ebts{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
.btn-buscar-dom{width:100%;padding:11px;border:none;border-radius:8px;background:linear-gradient(135deg,#FFC107,#F57C00);color:#222;font-weight:700;font-size:.85rem;cursor:pointer;margin:8px 0}
.btn-buscar-dom:hover{filter:brightness(1.05)}
.btn-buscar-dom:disabled{opacity:.6;cursor:default}
.dom-buscando{width:100%;padding:11px;border-radius:8px;background:#3a3a2a;color:#FFC107;font-weight:700;font-size:.85rem;text-align:center;margin:8px 0}
.dom-asignado{width:100%;padding:11px;border-radius:8px;background:#1b3a1b;color:#4CAF50;font-weight:700;font-size:.85rem;text-align:center;margin:8px 0}
.eb{padding:8px 0;border:none;border-radius:6px;cursor:pointer;font-size:.95rem;background:#f5f5f0;color:#aaa;opacity:.65;transition:all .15s}
.eb:hover{opacity:1}.eb.on{opacity:1}
.eb-activo.on{background:#4CAF50;color:#fff}.eb-preparando.on{background:#FF9800;color:#fff}
.eb-enviado.on{background:#2196F3;color:#fff}.eb-entregado.on{background:#9C27B0;color:#fff}.eb-cancelado.on{background:#f44336;color:#fff}
.empty{text-align:center;padding:50px 20px;color:#888;font-size:.95rem}
</style></head><body>
<div class="container">
<header>
  <div class="header-top">
    <h1>🍽️ Ipiales Delivery<span>Panel de Pedidos</span></h1>
    <div class="btns">
      <button class="btn btn-dark" onclick="cargarPedidos()">🔄 Actualizar</button>
      <button class="btn btn-out" onclick="window.location.href='/panel'">Salir</button>
    </div>
  </div>
  <div class="stats">
    <span id="s-hora">🕐 --:--</span>
    <span id="s-total">📊 Total: 0</span>
    <span id="s-activos">⚡ Activos: 0</span>
  </div>
</header>
<div class="cards-resumen">
  <div class="card-r"><span class="num" id="v-cant">0</span><span class="lbl">📦 Pedidos hoy</span></div>
  <div class="card-r total"><span class="num" id="v-total">$0</span><span class="lbl">💰 Total vendido hoy</span></div>
  <div class="card-r cancel"><span class="num" id="v-cancel">0</span><span class="lbl">❌ Cancelados hoy</span></div>
</div>
<div class="tabs">
  <button class="tab on" data-tab="todos" onclick="cambiarTab('todos')">📋 Todos <span id="c-todos"></span></button>
  <button class="tab" data-tab="preparacion" onclick="cambiarTab('preparacion')">📥 Recibidos <span id="c-prep"></span></button>
  <button class="tab" data-tab="enviados" onclick="cambiarTab('enviados')">🛵 Enviados <span id="c-env"></span></button>
  <button class="tab" data-tab="entregados" onclick="cambiarTab('entregados')">✅ Entregados <span id="c-ent"></span></button>
</div>
<div id="grid" class="grid"></div>
<div id="empty" class="empty" style="display:none">No hay pedidos en esta pestaña 😊</div>
<div style="text-align:center;margin-top:24px">
  <a href="/" style="color:#9a8a6b;font-size:.8rem;text-decoration:none">🌐 Ver página de información de CaZa Delivery</a>
</div>
</div>
<script>
const pw="{{PW}}";
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
let todos=[],tabActual="todos";
async function cargarPedidos(){
  try{
    const r=await fetch(`/api/pedidos?pw=${encodeURIComponent(pw)}`);
    if(r.status===403){window.location.href='/panel';return;}
    const d=await r.json();
    todos=d.pedidos;
    await cargarEstadosDom(todos);
    render();stats();contadores();ventasHoy();
  }catch(e){console.error(e);}
}
function esHoy(iso){
  try{
    const a=new Date(iso).toLocaleDateString('es-CO',{timeZone:'America/Bogota'});
    const b=new Date().toLocaleDateString('es-CO',{timeZone:'America/Bogota'});
    return a===b;
  }catch(e){return false;}
}
function ventasHoy(){
  const hoy=todos.filter(p=>esHoy(p.fecha));
  const vend=hoy.filter(p=>p.estado!=='cancelado');
  const canc=hoy.filter(p=>p.estado==='cancelado');
  const total=vend.reduce((a,p)=>a+(p.total||0),0);
  document.getElementById('v-cant').textContent=vend.length;
  document.getElementById('v-total').textContent='$'+Math.round(total).toLocaleString('es-CO');
  document.getElementById('v-cancel').textContent=canc.length;
}
function stats(){
  const ahora=new Date().toLocaleTimeString('es-CO',{hour:'2-digit',minute:'2-digit'});
  document.getElementById('s-hora').textContent=`🕐 ${ahora}`;
  document.getElementById('s-total').textContent=`📊 Total: ${todos.length}`;
  const act=todos.filter(p=>p.estado==='activo'||p.estado==='preparando').length;
  document.getElementById('s-activos').textContent=`⚡ Activos: ${act}`;
}
function contadores(){
  document.getElementById('c-todos').textContent=`(${todos.length})`;
  document.getElementById('c-prep').textContent=`(${todos.filter(p=>p.estado==='activo'||p.estado==='preparando').length})`;
  document.getElementById('c-env').textContent=`(${todos.filter(p=>p.estado==='enviado').length})`;
  document.getElementById('c-ent').textContent=`(${todos.filter(p=>p.estado==='entregado').length})`;
}
function filtrar(tab){
  if(tab==='preparacion')return todos.filter(p=>p.estado==='activo'||p.estado==='preparando');
  if(tab==='enviados')return todos.filter(p=>p.estado==='enviado');
  if(tab==='entregados')return todos.filter(p=>p.estado==='entregado');
  return todos;
}
function cambiarTab(tab){
  tabActual=tab;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('on'));
  document.querySelector(`.tab[data-tab="${tab}"]`).classList.add('on');
  render();
}
const EST_MAP={activo:'🆕 Activo',preparando:'📥 Pedido Recibido',enviado:'🛵 Enviado',entregado:'✅ Entregado',cancelado:'❌ Cancelado'};
const BTNS=[
  {k:'activo',i:'🆕',c:'eb-activo'},{k:'preparando',i:'📥',c:'eb-preparando'},
  {k:'enviado',i:'🛵',c:'eb-enviado'},{k:'entregado',i:'✅',c:'eb-entregado'},{k:'cancelado',i:'❌',c:'eb-cancelado'}
];
let estadosDom = {}; // pedido_id -> {asignado, nombre, buscando}

async function cargarEstadosDom(pedidos) {
  const domiciliosActivos = pedidos.filter(p => p.tipo === 'domicilio' && p.estado !== 'entregado' && p.estado !== 'cancelado');
  for (const p of domiciliosActivos) {
    try {
      const r = await fetch(`/api/pedidos/${p.id}/estado-domiciliario?pw=${encodeURIComponent(pw)}`);
      estadosDom[p.id] = await r.json();
    } catch(e) {}
  }
}

async function buscarDomiciliario(id) {
  const btn = document.getElementById(`btn-dom-${id}`);
  if (btn) { btn.disabled = true; btn.textContent = "🔍 Buscando..."; }
  try {
    const r = await fetch(`/api/pedidos/${id}/buscar-domiciliario`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({pw})
    });
    const d = await r.json();
    if (!d.ok) { alert(d.msg || "No se pudo buscar domiciliario"); }
    await cargarPedidos();
  } catch(e) {
    alert("Error al buscar domiciliario");
    cargarPedidos();
  }
}

function htmlBotonDom(p) {
  if (p.tipo !== 'domicilio') return '';
  const est = estadosDom[p.id] || {};
  if (est.asignado) {
    return `<div class="dom-asignado">🛵 Asignado a ${esc(est.nombre || 'domiciliario')}</div>`;
  }
  if (est.buscando) {
    return `<div class="dom-buscando">🔍 Buscando domiciliario...</div>`;
  }
  return `<button class="btn-buscar-dom" id="btn-dom-${p.id}" onclick="buscarDomiciliario('${p.id}')">🛵 Empezar a buscar domiciliario</button>`;
}

function render(){
  const g=document.getElementById('grid');
  const e=document.getElementById('empty');
  const lista=filtrar(tabActual);
  if(!lista.length){g.innerHTML='';e.style.display='block';return;}
  e.style.display='none';
  const orden = ['activo','preparando','enviado','entregado'];
  g.innerHTML=lista.map(p=>{
    const esFinal = p.estado==='entregado'||p.estado==='cancelado';
    const idxActual = orden.indexOf(p.estado);
    return `
    <div class="card">
      <div class="pid">#${esc(p.id)}</div>
      <div class="rest-tag">🍽️ ${esc(p.restaurante_nombre||'—')}</div>
      <div class="hora">🕐 ${esc(p.hora||'')}</div>
      <div class="tipo-tag">${p.tipo==='domicilio'?'🛵 Domicilio':'🏠 Recoger'}</div>
      <div class="cli">📱 ${esc(p.numero_cliente)}</div>
      ${p.cliente_nombre?`<div class="cli-nombre">👤 ${esc(p.cliente_nombre)}</div>`:''}
      <div class="dir">📍 ${esc(p.direccion)}</div>
      <div class="resumen">${esc(p.resumen||'')}</div>
      ${p.modificaciones&&p.modificaciones.length?`<div class="mods"><strong>📝 Modificaciones:</strong><br>${p.modificaciones.map(esc).join('<br>')}</div>`:''}
      ${p.quejas&&p.quejas.length?`<div class="quejas-box"><strong>⚠️ Quejas:</strong><br>${p.quejas.map(esc).join('<br>')}</div>`:''}
      <div class="est-lbl ${p.estado}">${EST_MAP[p.estado]||p.estado}</div>
      ${!esFinal ? htmlBotonDom(p) : ''}
      <div class="ebts">${BTNS.map(b=>{
        const idxBtn = orden.indexOf(b.k);
        const bloqueado = esFinal || (idxBtn < idxActual && b.k !== 'cancelado');
        return `<button class="eb ${b.c} ${p.estado===b.k?'on':''}"
          title="${b.k}"
          ${bloqueado ? 'disabled style="opacity:.25;cursor:not-allowed"' : ''}
          onclick="${bloqueado?'':''} cambiarEstado('${p.id}','${b.k}')">${b.i}</button>`;
      }).join('')}</div>
    </div>`;
  }).join('');
}
async function cambiarEstado(id,estado){
  await fetch(`/api/pedidos/${id}/estado`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pw,estado})});
  cargarPedidos();
}
cargarPedidos();
setInterval(cargarPedidos,6000);
</script></body></html>"""

# ── RUTAS ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def raiz():
    with open(os.path.join(STATIC_DIR, "landing.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/panel")
async def panel(pw: str = ""):
    if pw == PANEL_PASSWORD:
        return HTMLResponse(PANEL_HTML.replace("{{PW}}", PANEL_PASSWORD))
    return HTMLResponse(LOGIN_HTML)

@app.get("/api/pedidos")
async def api_pedidos(pw: str = ""):
    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    todos = get_todos_pedidos()
    # Enriquecer con nombre del cliente
    for p in todos:
        cli = get_cliente(p.get("numero_cliente", ""))
        p["cliente_nombre"] = cli["nombre"] if cli else ""
    return {"pedidos": todos}

def _aplicar_cambio_estado_pedido(pedido, nuevo):
    """Valida la transición y aplica el cambio de estado de un pedido ya cargado,
    notificando al cliente por WhatsApp. Reutilizada por el panel general y por
    el panel propio de cada restaurante."""
    pedido_id = pedido["id"]
    anterior = pedido["estado"]

    # Bloquear cambios desde estados finales
    if anterior in ["entregado", "cancelado"]:
        return {"ok": False, "msg": f"Este pedido ya fue {anterior} y no se puede modificar."}

    # Bloquear retrocesos de estado
    orden = ["activo", "preparando", "enviado", "entregado"]
    if nuevo in orden and anterior in orden:
        if orden.index(nuevo) < orden.index(anterior):
            return {"ok": False, "msg": f"No se puede volver de '{anterior}' a '{nuevo}'."}

    actualizar_estado_pedido(pedido_id, nuevo)
    numero = pedido["numero_cliente"]
    nombre_rest = pedido.get("restaurante_nombre", "")

    if nuevo == "enviado" and anterior != "enviado":
        msg = (f"🛵 *¡Tu pedido va en camino!*\n#{pedido_id} hacia {pedido['direccion']}.\n¡Gracias por pedir en {nombre_rest}! 🍔"
               if pedido["tipo"] == "domicilio"
               else f"✅ *¡Tu pedido está listo!*\n#{pedido_id} listo para recoger.\n¡Te esperamos en {nombre_rest}! 🍔")
        enviar_whatsapp(numero, msg)
    if nuevo == "entregado" and anterior != "entregado":
        enviar_whatsapp(numero, f"🙌 *¡Pedido entregado!* Esperamos que lo disfrutes.\n¡Gracias por elegir {nombre_rest}! 😊")
        enviar_whatsapp(numero, "⭐ ¿Cómo calificarías el servicio? Responde del 1 al 5 (puedes agregar un comentario si quieres).")
        clientes_esperando_calificacion[numero] = pedido_id
    if nuevo == "cancelado" and anterior != "cancelado":
        enviar_whatsapp(numero, f"❌ *Pedido #{pedido_id} cancelado.*\nSi tienes dudas contáctanos. ¡Hasta pronto! 🍔")
    return {"ok": True}

def _iniciar_busqueda_domiciliario(pedido):
    """Notifica a los domiciliarios disponibles para que acepten este pedido.
    Reutilizada por el panel general y por el panel propio de cada restaurante."""
    pedido_id = pedido["id"]
    if pedido.get("tipo") != "domicilio":
        return {"ok": False, "msg": "Este pedido no es de domicilio"}
    if pedido_ya_asignado(pedido_id):
        return {"ok": False, "msg": "Este pedido ya tiene domiciliario asignado"}

    agregar_pedido_pendiente(pedido)
    doms = get_domiciliarios_disponibles()
    if not doms:
        return {"ok": False, "msg": "No hay domiciliarios disponibles en este momento"}
    notificar_domiciliarios_whatsapp(pedido)
    return {"ok": True, "msg": f"Buscando entre {len(doms)} domiciliario(s) disponible(s)"}

@app.post("/api/pedidos/{pedido_id}/estado")
async def cambiar_estado(pedido_id: str, request: Request):
    body = await request.json()
    if body.get("pw") != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    nuevo = body.get("estado", "")
    if nuevo not in ["activo", "preparando", "enviado", "entregado", "cancelado"]:
        raise HTTPException(status_code=400)
    pedido = get_pedido_by_id(pedido_id)
    if not pedido:
        raise HTTPException(status_code=404)
    return _aplicar_cambio_estado_pedido(pedido, nuevo)

@app.post("/api/pedidos/{pedido_id}/buscar-domiciliario")
async def buscar_domiciliario(pedido_id: str, request: Request):
    body = await request.json()
    if body.get("pw") != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    pedido = get_pedido_by_id(pedido_id)
    if not pedido:
        raise HTTPException(status_code=404)
    return _iniciar_busqueda_domiciliario(pedido)

def _obtener_estado_domiciliario_pedido(pedido_id):
    """Reutilizada por el panel general y por el panel propio de cada restaurante."""
    asignado = pedido_ya_asignado(pedido_id)
    nombre = None
    if asignado:
        try:
            res = supabase.table("asignaciones").select("domiciliario_id").eq("pedido_id", pedido_id).execute()
            if res.data:
                dom_res = supabase.table("domiciliarios").select("nombre").eq("id", res.data[0]["domiciliario_id"]).execute()
                if dom_res.data:
                    nombre = dom_res.data[0]["nombre"]
        except Exception:
            pass
    buscando = pedido_id in _pedidos_pendientes
    return {"asignado": asignado, "nombre": nombre, "buscando": buscando}

@app.get("/api/pedidos/{pedido_id}/estado-domiciliario")
async def estado_domiciliario(pedido_id: str, pw: str = ""):
    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    return _obtener_estado_domiciliario_pedido(pedido_id)

# ── PANEL PROPIO POR RESTAURANTE ──────────────────────────────────────────────

@app.post("/panel-restaurante/login")
async def panel_restaurante_login(request: Request):
    body = await request.json()
    rest_key = get_restaurante_key_por_password(body.get("password", ""))
    if not rest_key:
        return {"ok": False, "msg": "Contraseña incorrecta"}
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "rest_session", generar_token_restaurante(rest_key),
        httponly=True, secure=True, samesite="strict",
        max_age=RESTAURANTE_SESSION_DIAS * 86400,
    )
    return resp

@app.post("/panel-restaurante/logout")
async def panel_restaurante_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("rest_session")
    return resp

@app.get("/panel-restaurante")
async def panel_restaurante_route(request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    r = _cache_restaurantes.get(rest_key) if rest_key else None
    if r:
        with open(os.path.join(STATIC_DIR, "panel_restaurante.html"), "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html.replace("{{NOMBRE_RESTAURANTE}}", r["nombre"]))
    return HTMLResponse(LOGIN_RESTAURANTE_HTML)

@app.get("/api/restaurante-panel/pedidos")
async def api_restaurante_panel_pedidos(request: Request, desde: str = "", hasta: str = ""):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    propios = get_pedidos_restaurante(rest_key, desde or None, hasta or None)
    for p in propios:
        cli = get_cliente(p.get("numero_cliente", ""))
        p["cliente_nombre"] = cli["nombre"] if cli else ""
        if p.get("tipo") == "domicilio" and p.get("estado") not in ["entregado", "cancelado"]:
            p["estado_domiciliario"] = _obtener_estado_domiciliario_pedido(p["id"])
    extra = _estado_extra[rest_key]
    r = get_restaurante(rest_key)
    estado = {
        "domicilio_activo": extra.get("domicilio_activo", True),
        "tiempo_espera": extra.get("tiempo_espera"),
        "notas": extra.get("notas", []),
        "abierto_forzado": extra.get("abierto_forzado", False),
        "abierto_ahora": esta_abierto(rest_key),
        "horario_semanal": r.get("horario_semanal") if r else None,
    }
    return {"pedidos": propios, "estado": estado}

@app.post("/api/restaurante-panel/pedidos/{pedido_id}/estado")
async def api_restaurante_panel_cambiar_estado(pedido_id: str, request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    nuevo = body.get("estado", "")
    if nuevo not in ["activo", "preparando", "enviado", "entregado", "cancelado"]:
        raise HTTPException(status_code=400)
    pedido = get_pedido_by_id(pedido_id)
    if not pedido or pedido.get("restaurante_id") != rest_key:
        raise HTTPException(status_code=404)
    return _aplicar_cambio_estado_pedido(pedido, nuevo)

@app.post("/api/restaurante-panel/pedidos/{pedido_id}/buscar-domiciliario")
async def api_restaurante_panel_buscar_domiciliario(pedido_id: str, request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    pedido = get_pedido_by_id(pedido_id)
    if not pedido or pedido.get("restaurante_id") != rest_key:
        raise HTTPException(status_code=404)
    return _iniciar_busqueda_domiciliario(pedido)

@app.post("/api/restaurante-panel/configuracion")
async def api_restaurante_panel_configuracion(request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    extra = _estado_extra[rest_key]
    if "domicilio_activo" in body:
        extra["domicilio_activo"] = bool(body["domicilio_activo"])
    if "tiempo_espera" in body:
        te = body["tiempo_espera"]
        extra["tiempo_espera"] = int(te) if te not in (None, "") else None
    if "abierto_forzado" in body:
        extra["abierto_forzado"] = bool(body["abierto_forzado"])
        extra["fecha_forzado"] = datetime.now(ZONA_HORARIA).date() if extra["abierto_forzado"] else None
    if "notas" in body:
        extra["notas"] = [n for n in body["notas"] if n]
    guardar_estado_extra(rest_key)
    return {"ok": True}

@app.get("/api/restaurante-panel/menu")
async def api_restaurante_panel_menu(request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        res = supabase.table("menu_items").select("*").eq("restaurante_id", rest_key).order("categoria").execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/restaurante-panel/menu/{item_id}/toggle")
async def api_restaurante_panel_toggle_item(item_id: int, request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        item_res = supabase.table("menu_items").select("restaurante_id").eq("id", item_id).execute()
        if not item_res.data or item_res.data[0]["restaurante_id"] != rest_key:
            raise HTTPException(status_code=404)
        supabase.table("menu_items").update({"activo": bool(body.get("activo", True))}).eq("id", item_id).execute()
        cargar_menu()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.get("/api/restaurante-panel/stats")
async def api_restaurante_panel_stats(request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        todos = get_pedidos_restaurante(rest_key, limite=500)
        hoy = date.today().isoformat()
        inicio_semana = (date.today() - timedelta(days=date.today().weekday())).isoformat()

        pedidos_hoy = [p for p in todos if p.get("fecha", "").startswith(hoy)]
        pedidos_semana = [p for p in todos if p.get("fecha", "") >= inicio_semana]

        ventas_hoy = sum(p.get("total", 0) for p in pedidos_hoy if p.get("estado") != "cancelado")
        ventas_semana = sum(p.get("total", 0) for p in pedidos_semana if p.get("estado") != "cancelado")

        calificaciones = [p["calificacion"] for p in todos if p.get("calificacion")]
        promedio_calificacion = round(sum(calificaciones) / len(calificaciones), 1) if calificaciones else None

        return {
            "ok": True,
            "stats": {
                "pedidos_hoy": len(pedidos_hoy),
                "ventas_hoy": ventas_hoy,
                "pedidos_semana": len(pedidos_semana),
                "ventas_semana": ventas_semana,
                "promedio_calificacion": promedio_calificacion,
                "total_calificaciones": len(calificaciones),
            }
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/restaurante-panel/horario")
async def api_restaurante_panel_horario(request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        supabase.table("restaurantes").update({"horario_semanal": body.get("horario_semanal")}).eq("id", rest_key).execute()
        cargar_restaurantes()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ── RUTAS DOMICILIARIOS ──────────────────────────────────────────────────────

@app.get("/domiciliarios")
async def domiciliarios_app():
    with open(os.path.join(STATIC_DIR, "domiciliarios.html"), "r") as f:
        return HTMLResponse(f.read())

@app.get("/api/domiciliarios/lista")
async def lista_domiciliarios():
    """Nombres de domiciliarios activos, para llenar el selector de login.
    No expone PIN ni teléfono."""
    return {"nombres": get_nombres_domiciliarios_activos()}

@app.post("/api/domiciliario/login")
async def login_domiciliario(request: Request):
    body = await request.json()
    nombre = body.get("nombre", "")
    pin = body.get("pin", "")
    dom = get_domiciliario_by_nombre(nombre)
    if not dom:
        return {"ok": False, "msg": "Domiciliario no encontrado"}
    if not dom.get("activo", True):
        return {"ok": False, "msg": "Cuenta desactivada"}
    if str(dom.get("pin", "")) != str(pin):
        return {"ok": False, "msg": "PIN incorrecto"}
    return {"ok": True, "dom_id": dom["id"], "nombre": dom["nombre"], "token": generar_token_dom(dom["id"])}

@app.post("/api/domiciliario/cambiar-pin")
async def cambiar_pin_domiciliario(request: Request):
    body = await request.json()
    nombre = body.get("nombre", "")
    pin_actual = body.get("pin_actual", "")
    pin_nuevo = body.get("pin_nuevo", "")
    dom = get_domiciliario_by_nombre(nombre)
    if not dom:
        return {"ok": False, "msg": "Domiciliario no encontrado"}
    if str(dom.get("pin", "")) != str(pin_actual):
        return {"ok": False, "msg": "PIN actual incorrecto"}
    if not pin_nuevo.isdigit() or len(pin_nuevo) != 6:
        return {"ok": False, "msg": "El nuevo PIN debe tener 6 dígitos"}
    supabase.table("domiciliarios").update({"pin": pin_nuevo}).eq("nombre", nombre).execute()
    return {"ok": True}

@app.get("/api/domiciliario/pedidos-pendientes")
async def pedidos_pendientes(dom_id: str = "", token: str = ""):
    dom = verificar_sesion_dom(dom_id, token)
    if not dom or not dom.get("disponible"):
        return {"pedido": None, "stats": {}}
    # Buscar pedido pendiente no asignado
    pedido_para_dom = None
    for pid, pedido in list(_pedidos_pendientes.items()):
        if not pedido_ya_asignado(pid):
            pedido_para_dom = pedido
            break
    # Stats del día
    try:
        from datetime import date
        hoy = date.today().isoformat()
        res = supabase.table("asignaciones").select("*")            .eq("domiciliario_id", dom["id"])            .gte("fecha", hoy).execute()
        pedidos_hoy = len(res.data or [])
    except Exception:
        pedidos_hoy = 0
    return {"pedido": pedido_para_dom, "stats": {"pedidos_hoy": pedidos_hoy}}

@app.post("/api/domiciliario/aceptar")
async def aceptar_pedido_dom(request: Request):
    body = await request.json()
    dom = verificar_sesion_dom(body.get("dom_id"), body.get("token"))
    if not dom:
        return {"ok": False, "msg": "Sesión inválida, vuelve a iniciar sesión"}
    nombre = dom["nombre"]
    pedido_id = body.get("pedido_id", "")
    if pedido_ya_asignado(pedido_id):
        return {"ok": False, "msg": "Este pedido ya fue tomado por otro domiciliario"}
    try:
        supabase.table("asignaciones").insert({
            "pedido_id": pedido_id,
            "domiciliario_id": dom["id"],
            "estado": "aceptado"
        }).execute()
        # Quitar de pendientes
        _pedidos_pendientes.pop(pedido_id, None)
        # Actualizar estado pedido
        actualizar_estado_pedido(pedido_id, "preparando")
        # Notificar al admin
        enviar_whatsapp(ADMIN_NUMBER,
            f"✅ *Pedido #{pedido_id} aceptado*\n"
            f"🛵 Domiciliario: *{nombre}*\n"
            f"👉 {os.getenv('PANEL_URL', '')}/panel")
        # Notificar al cliente
        pedido = get_pedido_by_id(pedido_id)
        if pedido:
            enviar_whatsapp(pedido["numero_cliente"],
                f"🛵 *¡Tu pedido #{pedido_id} fue aceptado!*\n"
                f"*{nombre}* está en camino con tu pedido.\n"
                f"¡Prepárate para recibirlo! 🍔")
        return {"ok": True}
    except Exception as e:
        # Si "asignaciones.pedido_id" tiene una restricción UNIQUE en Supabase,
        # dos domiciliarios aceptando al mismo tiempo terminan aquí: uno gana la
        # inserción y el otro recibe este error de duplicado en vez de asignarse igual.
        error_txt = str(e).lower()
        if "duplicate" in error_txt or "unique" in error_txt or "23505" in error_txt:
            return {"ok": False, "msg": "Este pedido ya fue tomado por otro domiciliario"}
        traceback.print_exc()
        return {"ok": False, "msg": "Error al asignar"}

@app.post("/api/domiciliario/entregado")
async def marcar_entregado_dom(request: Request):
    body = await request.json()
    dom = verificar_sesion_dom(body.get("dom_id"), body.get("token"))
    if not dom:
        return {"ok": False, "msg": "Sesión inválida, vuelve a iniciar sesión"}
    nombre = dom["nombre"]
    pedido_id = body.get("pedido_id", "")
    actualizar_estado_pedido(pedido_id, "entregado")
    pedido = get_pedido_by_id(pedido_id)
    if pedido:
        enviar_whatsapp(pedido["numero_cliente"],
            f"🙌 *¡Pedido #{pedido_id} entregado!*\n"
            f"Esperamos que lo disfrutes 😊\n"
            f"¡Gracias por pedir en Ipiales Delivery!")
        enviar_whatsapp(pedido["numero_cliente"],
            "⭐ ¿Cómo calificarías el servicio? Responde del 1 al 5 (puedes agregar un comentario si quieres).")
        clientes_esperando_calificacion[pedido["numero_cliente"]] = pedido_id
        enviar_whatsapp(ADMIN_NUMBER,
            f"✅ *Pedido #{pedido_id} entregado*\n🛵 Por: {nombre}")
    # Actualizar contador domiciliario
    supabase.table("domiciliarios").update({
        "pedidos_completados": (dom.get("pedidos_completados") or 0) + 1
    }).eq("id", dom["id"]).execute()
    return {"ok": True}

@app.post("/api/domiciliario/disponibilidad")
async def cambiar_disponibilidad(request: Request):
    body = await request.json()
    dom = verificar_sesion_dom(body.get("dom_id"), body.get("token"))
    if not dom:
        return {"ok": False, "msg": "Sesión inválida, vuelve a iniciar sesión"}
    disponible = body.get("disponible", True)
    supabase.table("domiciliarios").update({"disponible": disponible}).eq("id", dom["id"]).execute()
    return {"ok": True}

@app.get("/api/domiciliario/mis-pedidos")
async def mis_pedidos_dom(dom_id: str = "", token: str = ""):
    dom = verificar_sesion_dom(dom_id, token)
    if not dom:
        return {"pedidos": [], "stats": {}}
    try:
        # Pedidos asignados a este domiciliario que están en estado enviado/preparando
        res = supabase.table("asignaciones").select("pedido_id")            .eq("domiciliario_id", dom["id"]).execute()
        ids = [a["pedido_id"] for a in (res.data or [])]
        pedidos = []
        for pid in ids:
            p = get_pedido_by_id(pid)
            if p and p["estado"] in ["preparando", "enviado"]:
                pedidos.append(p)
        from datetime import date
        hoy = date.today().isoformat()
        res_hoy = supabase.table("asignaciones").select("*")            .eq("domiciliario_id", dom["id"]).gte("fecha", hoy).execute()
        return {"pedidos": pedidos, "stats": {"pedidos_hoy": len(res_hoy.data or [])}}
    except Exception:
        traceback.print_exc()
        return {"pedidos": [], "stats": {}}

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("Token invalido", status_code=403)

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    raw_body = await request.body()
    firma = request.headers.get("x-hub-signature-256", "")
    if not verificar_firma_webhook(raw_body, firma):
        print("🚫 Webhook rechazado: firma inválida (posible mensaje falsificado)")
        raise HTTPException(status_code=403, detail="Firma inválida")

    data = await request.json()
    print("DATOS:", data)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "ok"}

        mensaje = entry["messages"][0]

        if mensaje.get("type") != "text":
            numero = mensaje["from"]
            if numero != ADMIN_NUMBER:
                enviar_whatsapp(numero, "Por ahora solo puedo leer mensajes de texto 😊")
            return {"status": "ok"}

        message_id = mensaje.get("id", "")
        if message_id in mensajes_procesados:
            return {"status": "ok"}
        mensajes_procesados.add(message_id)
        if len(mensajes_procesados) > 500:
            ids = list(mensajes_procesados)
            mensajes_procesados.clear()
            mensajes_procesados.update(ids[-250:])

        numero  = mensaje["from"]
        texto   = mensaje["text"]["body"]
        texto_lower = texto.strip().lower()
        print(f"De {numero}: {texto}")

        # ── RATE LIMITING (solo clientes, no admin ni domiciliarios) ──
        if numero != ADMIN_NUMBER:
            dom = get_domiciliario_by_telefono(numero)
            if not dom:
                permitido, msg_error = verificar_rate_limit(numero)
                if not permitido:
                    enviar_whatsapp(numero, msg_error)
                    return {"status": "ok"}
                # Truncar mensajes muy largos
                if len(texto) > MAX_CHARS_MENSAJE:
                    texto = texto[:MAX_CHARS_MENSAJE]
                    texto_lower = texto.strip().lower()

        # ── DOMICILIARIO ──
        resp_dom = procesar_mensaje_domiciliario(numero, texto)
        if resp_dom:
            enviar_whatsapp(numero, resp_dom)
            return {"status": "ok"}

        # ── ADMIN ──
        if numero == ADMIN_NUMBER:
            resp_admin = procesar_comando_admin(texto)
            if resp_admin:
                enviar_whatsapp(numero, resp_admin)
                return {"status": "ok"}

        # ── CALIFICACIÓN DE SERVICIO ───────────────────────────────────────────
        if numero in clientes_esperando_calificacion:
            m = re.match(r"^\s*([1-5])(?:\s+(.*))?$", texto.strip(), re.DOTALL)
            if m:
                pedido_id_cal = clientes_esperando_calificacion.pop(numero)
                calificacion = int(m.group(1))
                comentario = (m.group(2) or "").strip() or None
                guardar_calificacion(pedido_id_cal, calificacion, comentario)
                enviar_whatsapp(numero, "¡Gracias por tu calificación! 🙏" + (" Tomamos nota de tu comentario." if comentario else ""))
                return {"status": "ok"}
            # Si no es un número del 1 al 5, no bloqueamos nada: dejamos la espera
            # activa y el mensaje sigue su flujo normal (ej. el cliente quiere pedir de nuevo).

        # ── REGISTRO DE CLIENTE ───────────────────────────────────────────────
        if numero in clientes_registrando:
            paso = clientes_registrando[numero]["paso"]
            if paso == "nombre":
                nombre_candidato = texto.strip()
                # Validar que sea un nombre real:
                # - máx 30 caracteres
                # - no contiene números
                # - no tiene más de 3 palabras
                # - no parece un saludo o pedido
                palabras_no_nombre = ["hola", "buenas", "quiero", "pedir", "dame", "salchipapa",
                                       "hamburguesa", "pizza", "pedido", "domicilio", "menu", "menú"]
                es_nombre_valido = (
                    len(nombre_candidato) <= 30 and
                    len(nombre_candidato.split()) <= 3 and
                    not any(p in nombre_candidato.lower() for p in palabras_no_nombre) and
                    not any(c.isdigit() for c in nombre_candidato)
                )
                if not es_nombre_valido:
                    enviar_whatsapp(numero,
                        "Necesito tu nombre para registrarte 😊\n"
                        "Por favor escribe solo tu nombre (ej: *Juan* o *Maria Lopez*)")
                    return {"status": "ok"}
                clientes_registrando[numero]["nombre"] = nombre_candidato
                clientes_registrando[numero]["paso"] = "direccion"
                enviar_whatsapp(numero,
                    f"¡Perfecto {nombre_candidato}! 😊\n\n"
                    f"¿Cuál es tu dirección habitual para domicilios?\n"
                    f"_(Escribe la dirección completa o *no tengo* si no tienes una fija)_")
                return {"status": "ok"}
            elif paso == "direccion":
                nombre = clientes_registrando[numero]["nombre"]
                direccion = texto.strip() if texto_lower != "no tengo" else ""
                crear_cliente(numero, nombre, direccion)
                clientes_registrando.pop(numero)
                enviar_whatsapp(numero,
                    f"✅ *¡Listo {nombre}, ya estás registrado!*\n\n"
                    f"Ahora elige un restaurante 👇")
                enviar_whatsapp(numero, lista_restaurantes())
                return {"status": "ok"}

        # ── VERIFICAR SI CLIENTE ESTÁ REGISTRADO ─────────────────────────────
        cliente = get_cliente(numero)
        if not cliente and numero != ADMIN_NUMBER:
            if numero not in clientes_registrando:
                clientes_registrando[numero] = {"paso": "nombre"}
                enviar_whatsapp(numero,
                    "¡Hola! 👋 Bienvenido a *Ipiales Delivery* 🍽️\n\n"
                    "Para comenzar necesito registrarte.\n"
                    "¿Cuál es tu nombre?")
                return {"status": "ok"}

        # ── PALABRAS CLAVE PARA VOLVER ────────────────────────────────────────
        palabras_cambio = ["inicio", "volver", "restaurantes", "cambiar", "cambiar restaurante", 
                           "otro restaurante", "ver restaurantes", "lista restaurantes",
                           "quiero otro restaurante", "otros restaurantes", "cuales restaurantes",
                           "cuáles restaurantes", "que restaurantes", "qué restaurantes",
                           "ver otros", "otros locales", "que hay", "qué hay"]
        if texto_lower in palabras_cambio or any(p in texto_lower for p in [
            "otro restaurante", "cambiar restaurante", "ver restaurantes", 
            "cuales son los restaurantes", "cuáles son los restaurantes",
            "que restaurantes hay", "qué restaurantes hay", "otros restaurantes",
            "quiero ir a otro", "cambiar de restaurante", "ver otros restaurantes"
        ]):
            cliente_restaurante.pop(numero, None)
            historial.pop(numero, None)
            clientes_eligiendo[numero] = True
            enviar_whatsapp(numero, lista_restaurantes())
            return {"status": "ok"}

        # ── CLIENTE SIN RESTAURANTE O ELIGIENDO ──────────────────────────────
        if numero not in cliente_restaurante or numero in clientes_eligiendo:
            clientes_eligiendo[numero] = True
            keys = list(_cache_restaurantes.keys())
            rest_key = None
            if texto_lower.isdigit():
                idx = int(texto_lower) - 1
                if 0 <= idx < len(keys):
                    rest_key = keys[idx]
            if rest_key is None:
                texto_normalizado = normalizar_texto(texto_lower)
                for k, r in _cache_restaurantes.items():
                    if normalizar_texto(r["nombre"]) in texto_normalizado or k.replace("_", " ") in texto_lower:
                        rest_key = k
                        break

            if rest_key is None:
                if numero not in cliente_restaurante:
                    nombre_cli = cliente["nombre"] if cliente else ""
                    saludo = f"¡Hola {nombre_cli}! 👋\n\n" if nombre_cli else ""
                    enviar_whatsapp(numero, saludo + lista_restaurantes())
                else:
                    enviar_whatsapp(numero, "Escribe el *nombre* o el *número* del restaurante (1, 2 o 3) 😊")
                return {"status": "ok"}

            if not esta_abierto(rest_key):
                r = _cache_restaurantes[rest_key]
                enviar_whatsapp(numero,
                    f"😔 *{r['nombre']}* está cerrado ahora.\n"
                    f"Horario: {r['hora_inicio']}:00 – {r['hora_fin']}:00.\n\n"
                    + lista_restaurantes())
                return {"status": "ok"}

            cliente_restaurante[numero] = rest_key
            clientes_eligiendo.pop(numero, None)
            # Bug 1 fix: limpiar TODO el estado anterior al entrar a un restaurante nuevo
            historial[numero] = []
            clientes_esperando_decision.pop(numero, None)
            r = _cache_restaurantes[rest_key]
            nombre_cli = cliente["nombre"] if cliente else ""
            enviar_whatsapp(numero,
                f"¡Perfecto{' ' + nombre_cli if nombre_cli else ''}! Estás en *{r['nombre']}* 🎉\n"
                f"📍 {r['direccion']}\n\n"
                f"¿Qué deseas pedir? 😊\n\n"
                f"_(Escribe *restaurantes* para volver a elegir)_")
            return {"status": "ok"}

        # ── YA ELIGIÓ RESTAURANTE ─────────────────────────────────────────────
        rest_key = cliente_restaurante[numero]
        r_actual = get_restaurante(rest_key)

        if r_actual is None:
            # El restaurante ya no existe (fue eliminado o cambiado de ID) - evita el crash
            cliente_restaurante.pop(numero, None)
            historial.pop(numero, None)
            clientes_eligiendo[numero] = True
            enviar_whatsapp(numero, "😔 Ese restaurante ya no está disponible.\n\n" + lista_restaurantes())
            return {"status": "ok"}

        if not esta_abierto(rest_key) and numero != ADMIN_NUMBER:
            enviar_whatsapp(numero, f"😔 *{r_actual['nombre']}* cerró. Horario: {r_actual['hora_inicio']}:00–{r_actual['hora_fin']}:00. ¡Hasta mañana! 🙏")
            cliente_restaurante.pop(numero, None)
            return {"status": "ok"}

        # ── RESPONDIENDO "MODIFICAR O NUEVO" ──────────────────────────────────
        saltar_palabras_clave = False
        if numero in clientes_esperando_decision:
            mensaje_original = clientes_esperando_decision.pop(numero)
            quiere_modificar = any(p in texto_lower for p in ["modificar", "el mismo", "ese pedido", "actualizar"])
            quiere_nuevo = any(p in texto_lower for p in ["nuevo", "otro pedido", "uno nuevo", "aparte"])

            if quiere_modificar:
                pedido = buscar_pedido_activo_cliente(numero)
                if pedido:
                    agregar_modificacion(pedido["id"], mensaje_original)
                    enviar_whatsapp(numero, "✅ Cambio anotado en tu pedido. ¡El equipo lo procesará! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER, f"📝 Pedido #{pedido['id']} modificado\n+{numero}\n{mensaje_original}")
                else:
                    enviar_whatsapp(numero, "Tu pedido anterior ya no está activo. ¿Quieres hacer uno nuevo?")
                return {"status": "ok"}
            elif quiere_nuevo:
                historial[numero] = []
                texto = mensaje_original
                texto_lower = texto.lower()
                saltar_palabras_clave = True
            else:
                clientes_esperando_decision[numero] = mensaje_original
                enviar_whatsapp(numero, "¿Quieres *modificar* tu pedido actual o hacer un *pedido nuevo*? Responde 'modificar' o 'nuevo'.")
                return {"status": "ok"}

        # ── OPCIONES ESPECIALES ───────────────────────────────────────────────
        if not saltar_palabras_clave:

            if any(p in texto_lower for p in ["cancelar pedido", "eliminar pedido", "quiero cancelar"]):
                pedido = buscar_pedido_activo_cliente(numero)
                if pedido:
                    actualizar_estado_pedido(pedido["id"], "cancelado")
                    enviar_whatsapp(numero, f"❌ Pedido #{pedido['id']} cancelado. ¡Hasta pronto! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ Pedido #{pedido['id']} cancelado por +{numero}")
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo para cancelar.")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["modificar pedido", "cambiar pedido", "agregar algo"]):
                pedido = buscar_pedido_activo_cliente(numero)
                if pedido:
                    agregar_modificacion(pedido["id"], texto)
                    enviar_whatsapp(numero, "✅ Modificación recibida. ¡El equipo lo procesará! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER, f"📝 Pedido #{pedido['id']} modificado\n+{numero}\n{texto}")
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo.")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["queja", "reclamación", "problema con mi pedido", "está mal"]):
                pedido = buscar_pedido_activo_cliente(numero)
                if pedido:
                    agregar_queja(pedido["id"], texto)
                    enviar_whatsapp(numero, "⚠️ Reclamación recibida. Nuestro equipo te contactará pronto. ¡Disculpa! 😟")
                    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ QUEJA #{pedido['id']}\n+{numero}\n{texto}")
                else:
                    enviar_whatsapp(numero, "Cuéntanos qué pasó 😊")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["el menú", "el menu", "menú", "menu", "carta", "pdf"]):
                r_menu = get_restaurante(rest_key)
                modo = r_menu.get("menu_modo") if r_menu else None
                url_menu = r_menu.get("menu_url") if r_menu else None
                if modo == "pdf" and url_menu:
                    enviar_whatsapp_documento(numero, url_menu, f"Menu-{r_menu['nombre']}.pdf", f"📋 Menú de {r_menu['nombre']}")
                elif modo == "link" and url_menu:
                    enviar_whatsapp(numero, f"📋 Puedes ver el menú completo de *{r_menu['nombre']}* aquí:\n{url_menu}")
                else:
                    enviar_menu_texto(numero, rest_key)
                enviar_whatsapp(numero, "¿Qué te gustaría pedir? 😊")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["ayuda", "help"]):
                enviar_whatsapp(numero,
                    "🤖 *¿Qué puedo hacer por ti?*\n\n"
                    "🍽️ *Restaurantes:*\n"
                    "• Escribe *restaurantes* → ver y cambiar de restaurante\n\n"
                    "📋 *Menú:*\n"
                    "• Escribe *menú* → ver el menú completo\n\n"
                    "🛒 *Pedidos:*\n"
                    "• Solo dime qué quieres pedir 😊\n"
                    "• Escribe *cancelar pedido* → cancelar tu pedido\n"
                    "• Escribe *modificar pedido* → cambiar algo de tu pedido\n"
                    "• Escribe *mis pedidos* → ver tus últimos 5 pedidos\n\n"
                    "⚠️ *Problemas:*\n"
                    "• Escribe *queja* → reportar un problema\n\n"
                    "👤 *Persona real:*\n"
                    "• Escribe *encargado* → te conectamos con el equipo"
                )
                return {"status": "ok"}

            if any(p in texto_lower for p in ["encargado", "hablar con", "persona real", "humano"]):
                r = _cache_restaurantes[rest_key]
                enviar_whatsapp(numero,
                    f"😊 Claro, te conecto con el equipo de *{r['nombre']}*.\n\n"
                    f"📍 {r['direccion']}\n\n"
                    f"Escribe *ayuda* para ver todas las opciones disponibles."
                )
                return {"status": "ok"}


            if any(p in texto_lower for p in ["mis pedidos", "mi historial", "mis órdenes"]):
                try:
                    res = supabase.table("pedidos").select("*")\
                        .eq("numero_cliente", numero)\
                        .order("fecha", desc=True).limit(5).execute()
                    hist = res.data or []
                    if not hist:
                        enviar_whatsapp(numero, "Aún no tienes pedidos registrados 😊")
                    else:
                        lineas = ["📦 *Tus últimos pedidos:*\n"]
                        for p in hist:
                            lineas.append(f"#{p['id']} — {p.get('restaurante_nombre','')} — {p['estado'].upper()} — {p['hora']}")
                        enviar_whatsapp(numero, "\n".join(lineas))
                except Exception:
                    enviar_whatsapp(numero, "No pude cargar tu historial ahora.")
                return {"status": "ok"}

            pedido_activo = buscar_pedido_activo_cliente(numero)
            palabras_pedido = ["quiero", "pedir", "me das", "dame", "una ", "un ", "dos ", "tres ",
                               "combo", "hamburguesa", "pizza", "salchipapa", "perro", "bebida", "gaseosa",
                               "pollo", "burger", "papas", "malteada", "limonada"]
            parece_pedido = any(p in texto_lower for p in palabras_pedido)
            if pedido_activo and parece_pedido:
                clientes_esperando_decision[numero] = texto
                enviar_whatsapp(numero,
                    f"👋 Ya tienes un pedido activo (#{pedido_activo['id']}) en *{pedido_activo.get('restaurante_nombre','')}*.\n"
                    f"¿Quieres *modificar* ese pedido o hacer un *pedido nuevo*?\nResponde 'modificar' o 'nuevo' 😊")
                return {"status": "ok"}

        # ── CLAUDE ────────────────────────────────────────────────────────────
        if numero not in historial:
            historial[numero] = []
        historial[numero].append({"role": "user", "content": texto})

        ai = anthropic.Anthropic(api_key=CLAUDE_KEY)
        resp = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=build_system_prompt(rest_key, cliente),
            messages=historial[numero],
        )
        texto_respuesta = resp.content[0].text
        print(f"Respuesta: {texto_respuesta}")
        historial[numero].append({"role": "assistant", "content": texto_respuesta})

        if len(historial[numero]) > 30:
            historial[numero] = historial[numero][-30:]

        enviar_whatsapp(numero, texto_respuesta)

        # Detectar cierre de pedido
        palabras_cierre = [
            "pedido recibido", "en camino", "listo para recoger",
            "pasamos a preparar", "empezamos a preparar", "estamos preparando",
            "pedido confirmado", "recibimos tu pedido", "ya recibimos",
            "tu pedido está", "hemos recibido tu pedido", "pedido anotado",
            "anotamos tu pedido", "ya está anotado"
        ]
        es_cierre = any(p in texto_respuesta.lower() for p in palabras_cierre)

        if es_cierre:
            resumen = texto_respuesta
            for msg in reversed(historial[numero]):
                if msg["role"] == "assistant" and "total" in msg["content"].lower() and "$" in msg["content"]:
                    resumen = msg["content"]
                    break
            # Construir texto completo de la conversación para extracción estructurada
            conversacion_txt = "\n".join(
                f"{'Cliente' if m['role'] == 'user' else 'Bot'}: {m['content']}"
                for m in historial[numero][-20:]
            )
            datos = extraer_pedido_estructurado(conversacion_txt, rest_key)
            pedido, es_nuevo = crear_pedido(numero, resumen, texto_respuesta, rest_key, datos)
            notificar_pedido_admin(numero, pedido, es_nuevo)
            notificar_pedido_restaurante(pedido, rest_key, es_nuevo)

    except Exception:
        traceback.print_exc()

    return {"status": "ok"}

# ══════════════════════════════════════════════════════════════════════════════
# ── PANEL ADMIN ───────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin2024")
ADMIN_SESSION_DIAS = 7

def generar_token_admin():
    expira = int(time.time()) + ADMIN_SESSION_DIAS * 86400
    firma = hmac.new(SESSION_SECRET.encode(), f"admin:{expira}".encode(), hashlib.sha256).hexdigest()
    return f"{expira}.{firma}"

def verificar_token_admin(token):
    if not token or "." not in token:
        return False
    try:
        expira_str, firma = token.split(".", 1)
        expira = int(expira_str)
    except ValueError:
        return False
    if time.time() > expira:
        return False
    firma_esperada = hmac.new(SESSION_SECRET.encode(), f"admin:{expira}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma_esperada, firma)

def check_admin(request: Request):
    if not verificar_token_admin(request.cookies.get("admin_session", "")):
        raise HTTPException(status_code=403, detail="No autorizado")

def get_restaurante_key_por_password(pw):
    """Encuentra qué restaurante tiene esta contraseña de panel propio (si alguno)."""
    if not pw:
        return None
    for key, r in _cache_restaurantes.items():
        if r.get("panel_password") and r.get("panel_password") == pw:
            return key
    return None

def password_panel_en_uso(pw, excluir_id=None):
    """Devuelve el nombre del restaurante que ya tiene esta contraseña de panel
    (si alguno), para no permitir que dos restaurantes la compartan."""
    if not pw:
        return None
    for key, r in _cache_restaurantes.items():
        if key == excluir_id:
            continue
        if r.get("panel_password") and r.get("panel_password") == pw:
            return r.get("nombre", key)
    return None

RESTAURANTE_SESSION_DIAS = 7

def generar_token_restaurante(rest_key):
    expira = int(time.time()) + RESTAURANTE_SESSION_DIAS * 86400
    firma = hmac.new(SESSION_SECRET.encode(), f"rest:{rest_key}:{expira}".encode(), hashlib.sha256).hexdigest()
    return f"{rest_key}:{expira}:{firma}"

def verificar_token_restaurante(token):
    """Si el token es válido devuelve el rest_key al que pertenece, si no None."""
    if not token or token.count(":") < 2:
        return None
    try:
        rest_key, expira_str, firma = token.split(":", 2)
        expira = int(expira_str)
    except ValueError:
        return None
    if time.time() > expira:
        return None
    firma_esperada = hmac.new(SESSION_SECRET.encode(), f"rest:{rest_key}:{expira}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(firma_esperada, firma):
        return None
    return rest_key

# ── APIs ADMIN: RESTAURANTES ──────────────────────────────────────────────────

@app.get("/api/admin/restaurantes")
async def admin_get_restaurantes(request: Request):
    check_admin(request)
    try:
        res = supabase.table("restaurantes").select("*").order("nombre").execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/admin/restaurantes")
async def admin_crear_restaurante(request: Request):
    body = await request.json()
    check_admin(request)
    panel_password = body.get("panel_password", "")
    en_uso = password_panel_en_uso(panel_password)
    if en_uso:
        return {"ok": False, "msg": f"Esa contraseña ya la está usando '{en_uso}'. Elige una diferente."}
    try:
        r = {
            "id": body["id"].lower().replace(" ", "_"),
            "nombre": body["nombre"],
            "direccion": body.get("direccion", ""),
            "hora_inicio": int(body.get("hora_inicio", 12)),
            "hora_fin": int(body.get("hora_fin", 23)),
            "domicilio_activo": True,
            "activo": True,
            "panel_password": body.get("panel_password", ""),
            "menu_modo": body.get("menu_modo", "texto"),
            "menu_url": body.get("menu_url", ""),
            "whatsapp_notificacion": body.get("whatsapp_notificacion", ""),
        }
        supabase.table("restaurantes").insert(r).execute()
        cargar_restaurantes()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.put("/api/admin/restaurantes/{rest_id}")
async def admin_editar_restaurante(rest_id: str, request: Request):
    body = await request.json()
    check_admin(request)
    if body.get("panel_password"):
        en_uso = password_panel_en_uso(body["panel_password"], excluir_id=rest_id)
        if en_uso:
            return {"ok": False, "msg": f"Esa contraseña ya la está usando '{en_uso}'. Elige una diferente."}
    try:
        datos = {k: v for k, v in body.items() if k not in ["pw", "id"]}
        if "hora_inicio" in datos: datos["hora_inicio"] = int(datos["hora_inicio"])
        if "hora_fin" in datos: datos["hora_fin"] = int(datos["hora_fin"])
        supabase.table("restaurantes").update(datos).eq("id", rest_id).execute()
        cargar_restaurantes()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.delete("/api/admin/restaurantes/{rest_id}")
async def admin_eliminar_restaurante(rest_id: str, request: Request):
    check_admin(request)
    try:
        supabase.table("menu_items").delete().eq("restaurante_id", rest_id).execute()
        supabase.table("restaurantes").delete().eq("id", rest_id).execute()
        cargar_restaurantes()
        cargar_menu()
        return {"ok": True}
    except Exception as e:
        error_txt = str(e).lower()
        if "foreign key" in error_txt or "still referenced" in error_txt:
            return {"ok": False, "msg": "No se puede eliminar: este restaurante ya tiene pedidos guardados en su historial. Usa 'Bloquear' en vez de 'Eliminar' para desactivarlo sin perder ese historial."}
        return {"ok": False, "msg": str(e)}

# ── APIs ADMIN: MENÚ ──────────────────────────────────────────────────────────

@app.get("/api/admin/menu/{rest_id}")
async def admin_get_menu(rest_id: str, request: Request):
    check_admin(request)
    try:
        res = supabase.table("menu_items").select("*").eq("restaurante_id", rest_id).order("categoria").execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/admin/menu")
async def admin_crear_item(request: Request):
    body = await request.json()
    check_admin(request)
    try:
        item = {
            "restaurante_id": body["restaurante_id"],
            "categoria": body["categoria"].lower().replace(" ", "_"),
            "descripcion": body["descripcion"],
            "activo": True,
        }
        supabase.table("menu_items").insert(item).execute()
        cargar_menu()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.put("/api/admin/menu/{item_id}")
async def admin_editar_item(item_id: int, request: Request):
    body = await request.json()
    check_admin(request)
    try:
        datos = {k: v for k, v in body.items() if k not in ["pw", "id"]}
        supabase.table("menu_items").update(datos).eq("id", item_id).execute()
        cargar_menu()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.delete("/api/admin/menu/{item_id}")
async def admin_eliminar_item(item_id: int, request: Request):
    check_admin(request)
    try:
        supabase.table("menu_items").delete().eq("id", item_id).execute()
        cargar_menu()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ── APIs ADMIN: DOMICILIARIOS ─────────────────────────────────────────────────

@app.get("/api/admin/domiciliarios")
async def admin_get_domiciliarios(request: Request):
    check_admin(request)
    try:
        res = supabase.table("domiciliarios").select("*").order("nombre").execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/admin/domiciliarios")
async def admin_crear_domiciliario(request: Request):
    body = await request.json()
    check_admin(request)
    try:
        dom = {
            "nombre": body["nombre"],
            "telefono": body["telefono"],
            "pin": body.get("pin", "123456"),
            "activo": True,
            "disponible": False,
        }
        supabase.table("domiciliarios").insert(dom).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.put("/api/admin/domiciliarios/{dom_id}")
async def admin_editar_domiciliario(dom_id: int, request: Request):
    body = await request.json()
    check_admin(request)
    try:
        datos = {k: v for k, v in body.items() if k not in ["pw", "id"]}
        supabase.table("domiciliarios").update(datos).eq("id", dom_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.delete("/api/admin/domiciliarios/{dom_id}")
async def admin_eliminar_domiciliario(dom_id: int, request: Request):
    check_admin(request)
    try:
        supabase.table("asignaciones").delete().eq("domiciliario_id", dom_id).execute()
        supabase.table("domiciliarios").delete().eq("id", dom_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ── APIs ADMIN: CLIENTES Y STATS ──────────────────────────────────────────────

@app.get("/api/admin/clientes")
async def admin_get_clientes(request: Request):
    check_admin(request)
    try:
        res = supabase.table("clientes").select("*").order("fecha_registro", desc=True).limit(100).execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.get("/api/admin/stats")
async def admin_get_stats(request: Request):
    check_admin(request)
    try:
        pedidos_res = supabase.table("pedidos").select("*").execute()
        todos = pedidos_res.data or []
        clientes_res = supabase.table("clientes").select("numero").execute()
        doms_res = supabase.table("domiciliarios").select("*").execute()
        rests_res = supabase.table("restaurantes").select("*").execute()

        from datetime import date
        hoy = date.today().isoformat()
        hoy_pedidos = [p for p in todos if p.get("fecha", "").startswith(hoy)]
        total_hoy = sum(p.get("total", 0) for p in hoy_pedidos if p.get("estado") != "cancelado")

        # Pedidos por restaurante
        por_rest = {}
        for p in todos:
            r = p.get("restaurante_nombre", "Sin nombre")
            por_rest[r] = por_rest.get(r, 0) + 1

        # Calificación promedio por restaurante
        calificaciones_por_rest = {}
        for p in todos:
            cal = p.get("calificacion")
            if cal:
                r = p.get("restaurante_nombre", "Sin nombre")
                calificaciones_por_rest.setdefault(r, []).append(cal)
        promedio_calificacion_por_rest = {
            r: round(sum(vals) / len(vals), 1) for r, vals in calificaciones_por_rest.items()
        }

        return {
            "ok": True,
            "stats": {
                "total_pedidos": len(todos),
                "pedidos_hoy": len(hoy_pedidos),
                "total_hoy": total_hoy,
                "total_clientes": len(clientes_res.data or []),
                "total_restaurantes": len(rests_res.data or []),
                "domiciliarios_disponibles": len([d for d in (doms_res.data or []) if d.get("disponible")]),
                "pedidos_por_restaurante": por_rest,
                "promedio_calificacion_por_restaurante": promedio_calificacion_por_rest,
            }
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ── PANEL ADMIN HTML ──────────────────────────────────────────────────────────

@app.post("/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    if body.get("password", "") != ADMIN_PASSWORD:
        return {"ok": False, "msg": "Contraseña incorrecta"}
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "admin_session", generar_token_admin(),
        httponly=True, secure=True, samesite="strict",
        max_age=ADMIN_SESSION_DIAS * 86400,
    )
    return resp

@app.post("/admin/logout")
async def admin_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("admin_session")
    return resp

@app.get("/admin")
async def admin_panel(request: Request):
    if verificar_token_admin(request.cookies.get("admin_session", "")):
        with open(os.path.join(STATIC_DIR, "admin.html"), "r") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — Ipiales Delivery</title>
<style>*{box-sizing:border-box}body{background:#0f0f0f;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#fff}
.box{background:#1a1a1a;border:1px solid #333;padding:32px 28px;border-radius:16px;text-align:center;width:90%;max-width:340px}
h1{font-size:1.3rem;margin-bottom:4px}h1 span{color:#FFC107}p{color:#888;margin-bottom:20px;font-size:.87rem}
input{width:100%;padding:12px;background:#222;border:1px solid #444;border-radius:10px;color:#fff;font-size:1rem;outline:none;margin-bottom:12px}
input:focus{border-color:#FFC107}button{width:100%;padding:12px;background:#FFC107;border:none;border-radius:10px;color:#1a1a1a;font-weight:700;font-size:1rem;cursor:pointer}
.err{color:#f44336;font-size:.82rem;margin:-6px 0 12px;display:none}
</style></head><body><div class="box"><h1>⚙️ ADMIN <span>PANEL</span></h1><p>Ipiales Delivery</p>
<form id="form-login"><input type="password" id="pw" placeholder="Contraseña admin" autofocus>
<div class="err" id="err">Contraseña incorrecta</div>
<button type="submit">Entrar</button></form></div>
<script>
document.getElementById('form-login').onsubmit = async function(e) {
  e.preventDefault();
  const err = document.getElementById('err');
  err.style.display = 'none';
  try {
    const r = await fetch('/admin/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: document.getElementById('pw').value})});
    const d = await r.json();
    if (d.ok) { window.location.href = '/admin'; }
    else { err.textContent = d.msg || 'Contraseña incorrecta'; err.style.display = 'block'; }
  } catch (e) { err.textContent = 'Error de conexión'; err.style.display = 'block'; }
};
</script>
</body></html>""")
