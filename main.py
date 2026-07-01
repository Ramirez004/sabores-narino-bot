from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import anthropic, requests, os, traceback, uuid, re
from dotenv import load_dotenv
from datetime import datetime, timedelta
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
PANEL_PASSWORD  = os.getenv("PANEL_PASSWORD", "ipiales2024")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")

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
cliente_restaurante     = {}
clientes_eligiendo      = {}
# registro en pasos: numero -> {"paso": "nombre"|"direccion"}
clientes_registrando    = {}

INTERVALO_CORTO_MINUTOS = 15

# ── SUPABASE: RESTAURANTES Y MENÚ ────────────────────────────────────────────
# Cache en memoria para no consultar Supabase en cada mensaje
_cache_restaurantes = {}
_cache_menu         = {}   # restaurante_id -> [{"categoria":..,"descripcion":..,"activo":..}]

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

def get_restaurante(rest_key):
    return _cache_restaurantes.get(rest_key)

def get_menu(rest_key):
    return _cache_menu.get(rest_key, [])

# Carga inicial al arrancar
cargar_restaurantes()
cargar_menu()

# Estado en memoria (domicilio forzado, espera, categorías desactivadas, notas)
# Se resetean si Railway reinicia, pero son cosas operativas del día
_estado_extra = {
    key: {
        "domicilio_activo": True,
        "tiempo_espera": None,
        "categorias_desactivadas": set(),
        "notas": [],
        "abierto_forzado": False,
        "fecha_forzado": None,
    }
    for key in ["las_bravas", "escarabajo", "monaco"]
}

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

    # ¿Ya tiene pedido activo? Lo actualiza
    activos = get_pedidos_activos(numero)
    if activos:
        pedido_id = activos[0]["id"]
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
    if not r.get("activo", True):
        return False
    return r["hora_inicio"] <= ahora.hour < r["hora_fin"]

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
    menu_activo = [i["descripcion"] for i in items if i["activo"] and i["categoria"] not in desact]
    notas = ("\nNOTAS DE HOY:\n- " + "\n- ".join(extra["notas"])) if extra.get("notas") else ""
    espera = f"\nTIEMPO DE ESPERA: {extra['tiempo_espera']} minutos." if extra.get("tiempo_espera") else ""
    dom = "Sí. Costo: $3.000." if extra.get("domicilio_activo", True) else "No disponible."

    saludo = ""
    if cliente:
        saludo = f"\nEl cliente se llama *{cliente['nombre']}* y su dirección habitual es *{cliente['direccion']}*. Salúdalo por su nombre."

    return f"""Eres el asistente virtual de *{r['nombre']}*, en {r['direccion']}, Ipiales.
HORARIO: {r['hora_inicio']}:00 – {r['hora_fin']}:00
DOMICILIO: {dom}
MÉTODOS DE PAGO: Nequi, Daviplata, transferencia, efectivo.
MENÚ:
{chr(10).join(menu_activo)}
{notas}{espera}{saludo}

INSTRUCCIONES:
- Habla amigable y natural como empleado real de {r['nombre']}.
- Si el cliente tiene dirección guardada y pide domicilio, úsala directamente sin preguntar de nuevo.
- Acumula todos los productos sin mostrar resumen parcial.
- NUNCA muestres resumen ni total hasta que el cliente diga "es todo", "listo", "eso sería" o similar.
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
        return "✅ Menú y restaurantes recargados desde la base de datos."

    # Identificar restaurante
    rest_key = None
    for key, r in _cache_restaurantes.items():
        if r["nombre"].lower() in t or key.replace("_", " ") in t or key in t:
            rest_key = key
            break

    if t.startswith("abre ") or t.startswith("abrir "):
        if rest_key:
            _estado_extra[rest_key]["abierto_forzado"] = True
            _estado_extra[rest_key]["fecha_forzado"] = datetime.now(ZONA_HORARIA).date()
            return f"✅ *{_cache_restaurantes[rest_key]['nombre']}* abierto hoy. Mañana se cierra solo."
        return "⚠️ No encontré el restaurante."

    if t.startswith("cierra ") or t.startswith("cerrar "):
        if rest_key:
            _estado_extra[rest_key]["abierto_forzado"] = False
            _estado_extra[rest_key]["fecha_forzado"] = None
            supabase.table("restaurantes").update({"activo": False}).eq("id", rest_key).execute()
            cargar_restaurantes()
            return f"✅ *{_cache_restaurantes[rest_key]['nombre']}* cerrado."
        return "⚠️ No encontré el restaurante."

    if rest_key is None:
        return None

    extra = _estado_extra[rest_key]
    nombre = _cache_restaurantes[rest_key]["nombre"]

    if "quita domicilio" in t or "desactiva domicilio" in t:
        extra["domicilio_activo"] = False
        return f"✅ Domicilio desactivado en *{nombre}*."
    if "activa domicilio" in t:
        extra["domicilio_activo"] = True
        return f"✅ Domicilio activado en *{nombre}*."

    m = re.search(r"espera\s+(\d+)", t)
    if m:
        extra["tiempo_espera"] = int(m.group(1))
        return f"✅ Espera de *{m.group(1)} min* en {nombre}."
    if "sin espera" in t or "quita espera" in t:
        extra["tiempo_espera"] = None
        return f"✅ Espera eliminada en {nombre}."

    if "borra notas" in t or "quita notas" in t:
        extra["notas"].clear()
        return f"✅ Notas borradas en *{nombre}*."
    if t.startswith("nota "):
        nota = re.sub(r"nota\s+", "", t).replace(_cache_restaurantes[rest_key]["nombre"].lower(), "").replace(rest_key.replace("_", " "), "").strip()
        extra["notas"].append(nota)
        return f"✅ Nota en *{nombre}*: '{nota}'"

    items = get_menu(rest_key)
    categorias = list({i["categoria"] for i in items})

    if t.startswith("quita ") or t.startswith("desactiva "):
        palabra = re.sub(r"(quita|desactiva)\s+", "", t).replace(_cache_restaurantes[rest_key]["nombre"].lower(), "").replace(rest_key.replace("_", " "), "").strip()
        for cat in categorias:
            if palabra in cat or cat in palabra:
                extra["categorias_desactivadas"].add(cat)
                return f"✅ *{cat}* desactivado en {nombre}."
        return f"⚠️ No encontré '{palabra}'. Categorías: {', '.join(categorias)}"

    if t.startswith("activa ") or t.startswith("pon "):
        palabra = re.sub(r"(activa|pon)\s+", "", t).replace(_cache_restaurantes[rest_key]["nombre"].lower(), "").replace(rest_key.replace("_", " "), "").strip()
        for cat in categorias:
            if palabra in cat or cat in palabra:
                extra["categorias_desactivadas"].discard(cat)
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
</div>
<script>
const pw="{{PW}}";
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
    return `<div class="dom-asignado">🛵 Asignado a ${est.nombre || 'domiciliario'}</div>`;
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
      <div class="pid">#${p.id}</div>
      <div class="rest-tag">🍽️ ${p.restaurante_nombre||'—'}</div>
      <div class="hora">🕐 ${p.hora||''}</div>
      <div class="tipo-tag">${p.tipo==='domicilio'?'🛵 Domicilio':'🏠 Recoger'}</div>
      <div class="cli">📱 ${p.numero_cliente}</div>
      ${p.cliente_nombre?`<div class="cli-nombre">👤 ${p.cliente_nombre}</div>`:''}
      <div class="dir">📍 ${p.direccion}</div>
      <div class="resumen">${p.resumen||''}</div>
      ${p.modificaciones&&p.modificaciones.length?`<div class="mods"><strong>📝 Modificaciones:</strong><br>${p.modificaciones.join('<br>')}</div>`:''}
      ${p.quejas&&p.quejas.length?`<div class="quejas-box"><strong>⚠️ Quejas:</strong><br>${p.quejas.join('<br>')}</div>`:''}
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
    return HTMLResponse("<h1>Ipiales Delivery Bot</h1><p><a href='/panel'>Ir al panel</a></p>")

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
    if nuevo == "cancelado" and anterior != "cancelado":
        enviar_whatsapp(numero, f"❌ *Pedido #{pedido_id} cancelado.*\nSi tienes dudas contáctanos. ¡Hasta pronto! 🍔")
    return {"ok": True}

@app.post("/api/pedidos/{pedido_id}/buscar-domiciliario")
async def buscar_domiciliario(pedido_id: str, request: Request):
    body = await request.json()
    if body.get("pw") != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    pedido = get_pedido_by_id(pedido_id)
    if not pedido:
        raise HTTPException(status_code=404)
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

@app.get("/api/pedidos/{pedido_id}/estado-domiciliario")
async def estado_domiciliario(pedido_id: str, pw: str = ""):
    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
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

# ── RUTAS DOMICILIARIOS ──────────────────────────────────────────────────────

@app.get("/domiciliarios")
async def domiciliarios_app():
    with open(os.path.join(STATIC_DIR, "domiciliarios.html"), "r") as f:
        return HTMLResponse(f.read())

@app.post("/api/domiciliario/login")
async def login_domiciliario(request: Request):
    body = await request.json()
    nombre = body.get("nombre", "")
    pin = body.get("pin", "")
    dom = get_domiciliario_by_nombre(nombre)
    if not dom:
        return {"ok": False, "msg": "Domiciliario no encontrado"}
    if str(dom.get("pin", "")) != str(pin):
        return {"ok": False, "msg": "PIN incorrecto"}
    return {"ok": True}

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
async def pedidos_pendientes(nombre: str = ""):
    dom = get_domiciliario_by_nombre(nombre)
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
    nombre = body.get("nombre", "")
    pedido_id = body.get("pedido_id", "")
    dom = get_domiciliario_by_nombre(nombre)
    if not dom:
        return {"ok": False, "msg": "Domiciliario no encontrado"}
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
    except Exception:
        traceback.print_exc()
        return {"ok": False, "msg": "Error al asignar"}

@app.post("/api/domiciliario/entregado")
async def marcar_entregado_dom(request: Request):
    body = await request.json()
    pedido_id = body.get("pedido_id", "")
    nombre = body.get("nombre", "")
    actualizar_estado_pedido(pedido_id, "entregado")
    pedido = get_pedido_by_id(pedido_id)
    if pedido:
        enviar_whatsapp(pedido["numero_cliente"],
            f"🙌 *¡Pedido #{pedido_id} entregado!*\n"
            f"Esperamos que lo disfrutes 😊\n"
            f"¡Gracias por pedir en Ipiales Delivery!")
        enviar_whatsapp(ADMIN_NUMBER,
            f"✅ *Pedido #{pedido_id} entregado*\n🛵 Por: {nombre}")
    # Actualizar contador domiciliario
    dom = get_domiciliario_by_nombre(nombre)
    if dom:
        supabase.table("domiciliarios").update({
            "pedidos_completados": (dom.get("pedidos_completados") or 0) + 1
        }).eq("id", dom["id"]).execute()
    return {"ok": True}

@app.post("/api/domiciliario/disponibilidad")
async def cambiar_disponibilidad(request: Request):
    body = await request.json()
    nombre = body.get("nombre", "")
    disponible = body.get("disponible", True)
    supabase.table("domiciliarios").update({"disponible": disponible}).eq("nombre", nombre).execute()
    return {"ok": True}

@app.get("/api/domiciliario/mis-pedidos")
async def mis_pedidos_dom(nombre: str = ""):
    dom = get_domiciliario_by_nombre(nombre)
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
            if texto_lower == "1": rest_key = keys[0]
            elif texto_lower == "2": rest_key = keys[1]
            elif texto_lower == "3": rest_key = keys[2]
            else:
                for k, r in _cache_restaurantes.items():
                    if r["nombre"].lower() in texto_lower or k.replace("_", " ") in texto_lower:
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

        if not esta_abierto(rest_key) and numero != ADMIN_NUMBER:
            r = _cache_restaurantes[rest_key]
            enviar_whatsapp(numero, f"😔 *{r['nombre']}* cerró. Horario: {r['hora_inicio']}:00–{r['hora_fin']}:00. ¡Hasta mañana! 🙏")
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

    except Exception:
        traceback.print_exc()

    return {"status": "ok"}
