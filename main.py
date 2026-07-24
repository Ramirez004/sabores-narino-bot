from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import anthropic, requests, os, traceback, uuid, re, unicodedata, hmac, hashlib, time, math, asyncio
from urllib.parse import quote
from dotenv import load_dotenv
from datetime import datetime, timedelta, date
import pytz
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")
images_dir = os.path.join(STATIC_DIR, "images")
os.makedirs(images_dir, exist_ok=True)
app.mount("/images", StaticFiles(directory=images_dir), name="images")

CLAUDE_KEY      = os.getenv("CLAUDE_KEY")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_MAIN_NUMBER = os.getenv("WHATSAPP_MAIN_NUMBER", "573107349485")  # número que se muestra en el botón "Pide Aquí"
STORAGE_BUCKET_FOTOS = "restaurantes-fotos"
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

# Límite de intentos en los logins de admin/restaurante/panel general (por IP,
# solo cuenta intentos FALLIDOS — a diferencia del rate limit de arriba, que
# cuenta todos los mensajes de WhatsApp).
_intentos_login = {}   # "tipo:ip" -> [timestamps de intentos fallidos]
_bloqueos_login = {}   # "tipo:ip" -> timestamp hasta cuando está bloqueado
INTENTOS_LOGIN_MAX = 5
INTENTOS_LOGIN_VENTANA_SEG = 300
INTENTOS_LOGIN_BLOQUEO_SEG = 300
clientes_esperando_decision = {}
clientes_esperando_calificacion = {}  # numero -> [pedido_id, ...] (cola: puede haber más de un pedido por calificar)
clientes_esperando_cual_pedido = {}  # numero -> {"accion": str, "texto": str, "pedidos": [pedido, ...]}
# Última ubicación de WhatsApp enviada por cada cliente (numero -> texto con link de maps)
ubicacion_reciente = {}
cliente_restaurante     = {}
clientes_eligiendo      = {}
# registro en pasos: numero -> {"paso": "nombre"|"direccion"}
clientes_registrando    = {}
codigo_aplicado = {}  # numero -> fila de codigos_descuento ya validada para este pedido en curso

# ── PERSISTENCIA DE SESIÓN ────────────────────────────────────────────────────
# Todos los dicts de arriba viven solo en memoria y se pierden con cada
# reinicio/redeploy de Railway. cargar_sesion/guardar_sesion los respaldan en
# la tabla sesiones_bot, sin cambiar cómo se leen/escriben en el resto del
# código — se llaman una vez al entrar y al salir del webhook (ver /webhook).
def cargar_sesion(numero):
    """Si este número no está en memoria todavía (primer mensaje suyo desde que
    arrancó este proceso — típicamente justo después de un reinicio), trae su
    historial y contexto guardados y llena los dicts en memoria con ellos."""
    if not numero or numero in historial:
        return
    try:
        res = supabase.table("sesiones_bot").select("*").eq("numero", numero).execute()
        if not res.data:
            return
        fila = res.data[0]
        historial[numero] = fila.get("historial") or []
        ctx = fila.get("contexto") or {}
        if "cliente_restaurante" in ctx: cliente_restaurante[numero] = ctx["cliente_restaurante"]
        if ctx.get("clientes_eligiendo"): clientes_eligiendo[numero] = True
        if "clientes_registrando" in ctx: clientes_registrando[numero] = ctx["clientes_registrando"]
        if "codigo_aplicado" in ctx: codigo_aplicado[numero] = ctx["codigo_aplicado"]
        if "ubicacion_reciente" in ctx: ubicacion_reciente[numero] = ctx["ubicacion_reciente"]
        if "clientes_esperando_decision" in ctx: clientes_esperando_decision[numero] = ctx["clientes_esperando_decision"]
        if "clientes_esperando_calificacion" in ctx: clientes_esperando_calificacion[numero] = ctx["clientes_esperando_calificacion"]
        if "clientes_esperando_cual_pedido" in ctx: clientes_esperando_cual_pedido[numero] = ctx["clientes_esperando_cual_pedido"]
    except Exception:
        traceback.print_exc()

def guardar_sesion(numero):
    """Guarda el estado actual de este número (historial + contexto) para que
    sobreviva un reinicio. Se llama al final de cada mensaje procesado."""
    if not numero:
        return
    try:
        contexto = {}
        if numero in cliente_restaurante: contexto["cliente_restaurante"] = cliente_restaurante[numero]
        if numero in clientes_eligiendo: contexto["clientes_eligiendo"] = True
        if numero in clientes_registrando: contexto["clientes_registrando"] = clientes_registrando[numero]
        if numero in codigo_aplicado: contexto["codigo_aplicado"] = codigo_aplicado[numero]
        if numero in ubicacion_reciente: contexto["ubicacion_reciente"] = ubicacion_reciente[numero]
        if numero in clientes_esperando_decision: contexto["clientes_esperando_decision"] = clientes_esperando_decision[numero]
        if numero in clientes_esperando_calificacion: contexto["clientes_esperando_calificacion"] = clientes_esperando_calificacion[numero]
        if numero in clientes_esperando_cual_pedido: contexto["clientes_esperando_cual_pedido"] = clientes_esperando_cual_pedido[numero]
        supabase.table("sesiones_bot").upsert({
            "numero": numero,
            "historial": historial.get(numero, []),
            "contexto": contexto,
            "actualizado_en": datetime.now(ZONA_HORARIA).isoformat(),
        }).execute()
    except Exception:
        traceback.print_exc()

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
  "subtotal": numero_entero_sin_puntos_ni_simbolos,
  "total": numero_entero_sin_puntos_ni_simbolos,
  "resumen_items": "lista de productos pedidos con cantidades y precios, en texto plano",
  "metodo_pago": "efectivo", "nequi", "bre_b" o null si no quedó claro
}}

Reglas:
- Si el cliente mencionó cualquier lugar de entrega (casa, edificio, barrio, calle, conjunto, punto de referencia), tipo es "domicilio".
- Si el cliente envió su ubicación (aparece un link de maps.google.com en la conversación), tipo es "domicilio" y usa ese link completo (con el nombre del lugar si lo hay) como direccion.
- Si el cliente dijo explícitamente que recoge en el local, o nunca mencionó dirección y el bot preguntó y confirmó "recoger", tipo es "recoger".
- Si hay duda, prioriza "domicilio" si se mencionó algún lugar.
- subtotal es el valor de SOLO los productos pedidos, sin domicilio y sin descuentos.
- total debe ser el monto final incluyendo domicilio si aplica (el que efectivamente paga el cliente).
- metodo_pago: "efectivo" si el cliente dijo que paga en efectivo/cash; "nequi" si mencionó Nequi; "bre_b" si mencionó Bre-B o "llave"; null si nunca se mencionó cómo paga.

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
        direccion = (datos_estructurados.get("direccion") or "").strip()
        if tipo == "recoger":
            direccion = "En local"
        elif not direccion:
            # La extracción no detectó dirección: primero la ubicación de
            # WhatsApp que haya enviado en esta conversación, luego la que
            # registró al inscribirse, y solo al final "Ver resumen"
            direccion = ubicacion_reciente.get(numero, "")
            if not direccion:
                cli = get_cliente(numero)
                direccion = (cli.get("direccion") or "").strip() if cli else ""
            if not direccion:
                direccion = "Ver resumen"
        try:
            total = int(datos_estructurados.get("total", 0))
        except (ValueError, TypeError):
            total = 0
        if not total:
            m = re.search(r"Total:?\s*\$?\s?([\d.,]+)", resumen, re.IGNORECASE)
            if m:
                total = int(m.group(1).replace(".", "").replace(",", ""))
        try:
            subtotal = int(datos_estructurados.get("subtotal", 0))
        except (ValueError, TypeError):
            subtotal = 0
        productos_texto = (datos_estructurados.get("resumen_items") or "").strip()
        metodo_pago = datos_estructurados.get("metodo_pago")
        if metodo_pago not in ("efectivo", "nequi", "bre_b"):
            metodo_pago = None
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
                direccion = ubicacion_reciente.get(numero, "")
                if not direccion:
                    cli = get_cliente(numero)
                    direccion = (cli.get("direccion") or "").strip() if cli else ""
                if not direccion:
                    direccion = "Ver resumen"
        total = 0
        m = re.search(r"Total:?\s*\$?\s?([\d.,]+)", resumen, re.IGNORECASE)
        if m:
            total = int(m.group(1).replace(".", "").replace(",", ""))
        subtotal = 0
        productos_texto = ""
        metodo_pago = None

    # El pago en efectivo (o sin especificar) no requiere confirmación;
    # Nequi y Bre-B quedan pendientes hasta que el restaurante confirme que llegó la plata.
    pago_confirmado = metodo_pago not in ("nequi", "bre_b")

    # Código de descuento ya validado en la conversación (si el cliente escribió uno)
    codigo_fila = codigo_aplicado.get(numero)
    codigo_texto_usado = codigo_fila["codigo"] if codigo_fila else None

    ahora = datetime.now(ZONA_HORARIA)

    # ¿Ya tiene un pedido en estado "activo" (aún no aceptado) EN ESTE MISMO RESTAURANTE?
    # Lo actualiza. Si el pedido activo es de OTRO restaurante (el cliente cambió de
    # restaurante antes de que le aceptaran el primero) o ya está "preparando" (ya
    # aceptado), NO se toca: se crea un pedido nuevo aparte, para no mezclar el pedido
    # de un restaurante con el de otro ni perder/retroceder el que ya está en curso.
    activos = get_pedidos_activos(numero)
    pedido_para_actualizar = next(
        (p for p in activos if p["estado"] == "activo" and p.get("restaurante_id") == rest_key),
        None,
    )

    # Si hay un código de descuento para este pedido (recién validado, o ya guardado
    # de una edición anterior del mismo pedido "activo"), recalculamos el total
    # NOSOTROS MISMOS con el subtotal extraído — nunca confiamos en que el resumen de
    # texto de Claude haya hecho bien la resta o el porcentaje. Así el monto que
    # queda guardado (y que ve el restaurante) siempre es exacto, sin importar lo
    # que haya mostrado el chat en ese momento.
    # descuento_monto queda guardado en el pedido para que los paneles puedan
    # mostrar "cuánto fue el descuento" sin tener que volver a calcularlo.
    descuento_monto = 0
    codigo_para_total = codigo_fila
    if not codigo_para_total and pedido_para_actualizar and pedido_para_actualizar.get("codigo_descuento"):
        codigo_para_total = buscar_codigo_descuento(pedido_para_actualizar["codigo_descuento"])
    if codigo_para_total and subtotal > 0:
        monto_min = codigo_para_total.get("monto_minimo") or 0
        if subtotal >= monto_min:
            valor_desc = codigo_para_total.get("valor", 0) or 0
            es_porcentaje = codigo_para_total.get("tipo") == "porcentaje"
            aplica_a = codigo_para_total.get("aplica_a") or "total"
            costo_dom = costo_domicilio(r) if tipo == "domicilio" else 0
            if aplica_a == "domicilio":
                if tipo == "domicilio":
                    monto_desc = costo_dom * (valor_desc / 100) if es_porcentaje else valor_desc
                    monto_desc = min(monto_desc, costo_dom)
                    total = round(subtotal + costo_dom - monto_desc)
                    descuento_monto = round(monto_desc)
                # Si es "recoger", este código no aplica: el total queda como el subtotal normal.
            else:
                monto_desc = subtotal * (valor_desc / 100) if es_porcentaje else valor_desc
                monto_desc = min(monto_desc, subtotal)
                total = round(subtotal - monto_desc + costo_dom)
                descuento_monto = round(monto_desc)

    if pedido_para_actualizar:
        pedido_id = pedido_para_actualizar["id"]
        datos_actualizar = {
            "resumen": resumen,
            "productos": productos_texto,
            "subtotal": subtotal,
            "descuento_monto": descuento_monto,
            "total": total,
            "tipo": tipo,
            "direccion": direccion,
            "estado": "activo",
            "metodo_pago": metodo_pago,
            "pago_confirmado": pago_confirmado,
            "hora": ahora.strftime("%I:%M %p"),
            "fecha": ahora.isoformat(),
        }
        # Solo tocamos codigo_descuento si hay uno nuevo por guardar — así no
        # volvemos a descontar un uso si el cliente sigue editando el mismo pedido.
        # El uso se descuenta AHORA (al confirmarse), no al entregarse, para topar
        # bien cuántos pedidos pueden reservar el código a la vez. Si el pedido se
        # cancela después, se le devuelve el uso (ver restaurar_uso_codigo_si_aplica).
        if codigo_texto_usado and not pedido_para_actualizar.get("codigo_descuento"):
            datos_actualizar["codigo_descuento"] = codigo_texto_usado
            consumir_uso_codigo(codigo_fila)
            codigo_aplicado.pop(numero, None)
        supabase.table("pedidos").update(datos_actualizar).eq("id", pedido_id).execute()
        verificar_subtotal(rest_key, productos_texto, subtotal, pedido_id)
        return get_pedido_by_id(pedido_id), False

    pedido_id = str(uuid.uuid4())[:8].upper()
    pedido = {
        "id": pedido_id,
        "numero_cliente": numero,
        "restaurante_id": rest_key,
        "restaurante_nombre": r["nombre"] if r else rest_key,
        "resumen": resumen,
        "productos": productos_texto,
        "subtotal": subtotal,
        "descuento_monto": descuento_monto,
        "total": total,
        "tipo": tipo,
        "direccion": direccion,
        "estado": "activo",
        "modificaciones": [],
        "quejas": [],
        "metodo_pago": metodo_pago,
        "pago_confirmado": pago_confirmado,
        "codigo_descuento": codigo_texto_usado,
        "hora": ahora.strftime("%I:%M %p"),
        "fecha": ahora.isoformat(),
    }
    supabase.table("pedidos").insert(pedido).execute()
    if codigo_fila:
        consumir_uso_codigo(codigo_fila)
        codigo_aplicado.pop(numero, None)
    verificar_subtotal(rest_key, productos_texto, subtotal, pedido_id)
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

def resolver_pedido_o_preguntar(numero, accion, texto_original):
    """Si el cliente tiene un solo pedido activo/preparando, lo devuelve directo.
    Si tiene varios (ej. uno en cada restaurante), le pregunta cuál y guarda la
    acción pendiente para cuando responda. Devuelve (pedido, ya_resuelto):
    - (pedido, True): actúa sobre este pedido ya mismo.
    - (None, True): no tiene ningún pedido activo.
    - (None, False): se le preguntó cuál — el llamador solo debe retornar."""
    activos = get_pedidos_activos(numero)
    if not activos:
        return None, True
    if len(activos) == 1:
        return activos[0], True
    opciones_txt = "\n".join(f"{i+1}. #{p['id']} en {p.get('restaurante_nombre') or '—'}" for i, p in enumerate(activos))
    clientes_esperando_cual_pedido[numero] = {"accion": accion, "texto": texto_original, "pedidos": activos}
    enviar_whatsapp(numero, f"Tienes varios pedidos activos, ¿cuál? Responde con el número:\n{opciones_txt}")
    return None, False

def ejecutar_cancelar_pedido(pedido, numero):
    actualizar_estado_pedido(pedido["id"], "cancelado")
    enviar_whatsapp(numero, f"❌ Pedido #{pedido['id']} cancelado. ¡Hasta pronto! 🍔")
    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ Pedido #{pedido['id']} cancelado por +{numero}")
    restaurar_uso_codigo_si_aplica(pedido)
    r_pedido = get_restaurante(pedido.get("restaurante_id", ""))
    notificar_restaurante(r_pedido,
        f"❌ *Pedido #{pedido['id']} CANCELADO por el cliente*\n"
        f"👤 +{numero}\n\n"
        f"Si ya lo estabas preparando, puedes detenerlo."
    )

def ejecutar_modificar_pedido(pedido, numero, texto):
    agregar_modificacion(pedido["id"], texto)
    enviar_whatsapp(ADMIN_NUMBER, f"📝 Pedido #{pedido['id']} modificado\n+{numero}\n{texto}")
    r_pedido = get_restaurante(pedido.get("restaurante_id", ""))

    if pedido["estado"] == "activo":
        # Aún no lo acepta el restaurante: le llega el aviso directo por WhatsApp.
        notificar_restaurante(r_pedido,
            f"🔄 *Pedido #{pedido['id']} MODIFICADO*\n"
            f"👤 +{numero}\n"
            f"────────────────\n"
            f"{texto}\n"
            f"────────────────\n"
            f"👉 Entra a tu panel: {os.getenv('PANEL_URL', '')}/panel-restaurante"
        )
        enviar_whatsapp(numero, "✅ Modificación recibida. ¡El equipo lo procesará! 🍔")
    else:
        # Ya está "preparando" (aceptado): dejamos la nota por si alcanzan a
        # verla, pero avisamos al cliente que puede no llegar a tiempo y le
        # damos el contacto directo del restaurante para hablar con alguien real.
        # Si hay varios números configurados, el link de contacto directo usa
        # solo el primero (un link de WhatsApp no puede apuntar a varios).
        primer_numero_rest = ((r_pedido.get("whatsapp_notificacion") or "").split(",")[0].strip()) if r_pedido else ""
        if primer_numero_rest:
            aviso_contacto = f"Si es urgente, escríbeles directo: https://wa.me/{primer_numero_rest}"
        else:
            aviso_contacto = "Si es urgente, escribe *encargado* para que te ayude nuestro equipo."
        enviar_whatsapp(numero,
            f"⚠️ Tu pedido #{pedido['id']} ya fue aceptado y se está preparando, así que puede que no "
            f"alcancen a ver este cambio a tiempo. Igual dejamos la nota registrada. {aviso_contacto}"
        )

def ejecutar_queja_pedido(pedido, numero, texto):
    agregar_queja(pedido["id"], texto)
    enviar_whatsapp(numero, "⚠️ Reclamación recibida. Nuestro equipo te contactará pronto. ¡Disculpa! 😟")
    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ QUEJA #{pedido['id']}\n+{numero}\n{texto}")

# ── HELPERS RESTAURANTES ──────────────────────────────────────────────────────

def normalizar_texto(s):
    """Quita tildes y pasa a minúsculas para comparar nombres sin depender de acentos."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()

def precios_menu(rest_key):
    """{nombre_normalizado: precio} de cada producto activo, sacando el precio
    del "$X.XXX" al final de la descripción (el menú no tiene un campo de
    precio separado, va embebido en el texto)."""
    precios = {}
    for item in get_menu(rest_key):
        if not item.get("activo", True):
            continue
        desc = item.get("descripcion") or ""
        m = re.findall(r"\$\s?([\d.]+)", desc)
        if not m:
            continue
        try:
            precio = int(m[-1].replace(".", ""))
        except (ValueError, TypeError):
            continue
        nombre = re.sub(r"\$\s?[\d.]+.*$", "", desc).strip(" -—")
        if nombre:
            precios[normalizar_texto(nombre)] = precio
    return precios

def _extraer_cantidad(linea):
    """Busca "2x" o "x2" en la línea de un producto; si no hay ninguna, asume 1."""
    m = re.search(r"(?:^|\s)(\d+)\s*[xX]|[xX]\s*(\d+)(?:\s|$)", linea)
    if m:
        try:
            return int(m.group(1) or m.group(2))
        except (ValueError, TypeError):
            pass
    return 1

def verificar_subtotal(rest_key, productos_texto, subtotal_reportado, pedido_id):
    """Suma los precios reales del menú para los productos del pedido y, si
    difiere bastante de lo que calculó Claude, avisa al admin para que revise
    a mano. NO bloquea ni corrige el pedido — el menú es texto libre y el
    match no siempre es exacto, así que si hay cualquier línea que no se
    pueda emparejar con confianza, simplemente no se avisa (para no generar
    falsas alarmas)."""
    if not productos_texto or not subtotal_reportado:
        return
    precios = precios_menu(rest_key)
    if not precios:
        return
    lineas = [l.strip() for l in productos_texto.split("\n") if l.strip()]
    if not lineas:
        return
    suma = 0
    for linea in lineas:
        linea_norm = normalizar_texto(linea)
        candidatos = [precio for nombre, precio in precios.items() if nombre in linea_norm]
        if len(candidatos) != 1:
            return
        suma += candidatos[0] * _extraer_cantidad(linea)
    diferencia = abs(suma - subtotal_reportado)
    umbral = max(2000, subtotal_reportado * 0.05)
    if diferencia > umbral:
        try:
            suma_txt = f"{suma:,.0f}".replace(",", ".")
            reportado_txt = f"{subtotal_reportado:,.0f}".replace(",", ".")
            enviar_whatsapp(ADMIN_NUMBER,
                f"⚠️ *Posible error de cálculo* en el pedido #{pedido_id}\n"
                f"El bot calculó subtotal: ${reportado_txt}\n"
                f"Sumando el menú da: ${suma_txt}\n"
                f"Revisa el pedido a mano.")
        except Exception:
            traceback.print_exc()

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

def metodos_pago_texto(r):
    """Arma el texto de métodos de pago disponibles para ESTE restaurante,
    a partir de los campos opcionales nequi_numero / llave_bre_b que haya
    configurado en el panel admin. Efectivo siempre está disponible."""
    partes = ["Efectivo"]
    nequi = (r.get("nequi_numero") or "").strip() if r else ""
    if nequi:
        partes.append(f"Nequi ({nequi})")
    bre_b = (r.get("llave_bre_b") or "").strip() if r else ""
    if bre_b:
        partes.append(f"Bre-B / llave ({bre_b})")
    return ", ".join(partes)

def costo_domicilio(r):
    """Valor del domicilio configurado por el restaurante en el panel admin
    (mismo número que paga el cliente y que gana el domiciliario por la entrega).
    Si no se configuró, usa $3.000 por defecto."""
    try:
        return int(r.get("costo_domicilio") or 3000) if r else 3000
    except (ValueError, TypeError):
        return 3000

def extraer_lat_lng(texto):
    """Saca (lat, lng) como floats de un link tipo https://maps.google.com/?q=lat,lng
    (el mismo formato que usa el bot al recibir una ubicación de WhatsApp, y el
    que se pide pegar en el panel admin para la ubicación del restaurante).
    Devuelve (None, None) si no encuentra coordenadas."""
    if not texto:
        return None, None
    m = re.search(r"q=(-?\d+\.?\d*),(-?\d+\.?\d*)", texto)
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except (ValueError, TypeError):
        return None, None

def distancia_km(lat1, lng1, lat2, lng2):
    """Distancia en línea recta entre dos coordenadas (fórmula de Haversine),
    sin depender de ningún servicio externo de mapas."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def calcular_ganancias_dom(dom_id):
    """Suma el costo_domicilio de cada pedido ENTREGADO por este domiciliario,
    hoy y en los últimos 7 días (para mostrarle cuánto ha ganado)."""
    ahora = datetime.now(ZONA_HORARIA)
    hoy_str = ahora.date().isoformat()
    hace_7_dias = (ahora - timedelta(days=7)).isoformat()
    try:
        res = supabase.table("asignaciones").select("pedido_id")\
            .eq("domiciliario_id", dom_id).gte("fecha", hace_7_dias).execute()
        ids = [a["pedido_id"] for a in (res.data or [])]
    except Exception:
        return {"ganancia_hoy": 0, "ganancia_semana": 0}
    ganancia_hoy = 0
    ganancia_semana = 0
    for pid in ids:
        p = get_pedido_by_id(pid)
        if not p or p.get("estado") != "entregado":
            continue
        valor = costo_domicilio(get_restaurante(p.get("restaurante_id", "")))
        ganancia_semana += valor
        try:
            fecha_pedido = datetime.fromisoformat(p["fecha"]).astimezone(ZONA_HORARIA).date().isoformat()
        except Exception:
            fecha_pedido = None
        if fecha_pedido == hoy_str:
            ganancia_hoy += valor
    return {"ganancia_hoy": ganancia_hoy, "ganancia_semana": ganancia_semana}

def buscar_codigo_descuento(codigo_texto):
    """Busca un código de descuento por su texto, sin importar mayúsculas/minúsculas."""
    try:
        res = supabase.table("codigos_descuento").select("*").ilike("codigo", codigo_texto).execute()
        return res.data[0] if res.data else None
    except Exception:
        traceback.print_exc()
        return None

def cliente_ya_uso_codigo(numero, codigo_texto):
    """Revisa el historial de pedidos del cliente para ver si ya usó este código
    antes (en cualquier restaurante), sin necesitar una tabla aparte. No cuenta
    pedidos cancelados, porque ahí el cliente no llegó a beneficiarse de verdad."""
    try:
        res = supabase.table("pedidos").select("id,estado,codigo_descuento")\
            .eq("numero_cliente", numero).ilike("codigo_descuento", codigo_texto).execute()
        return any(p.get("estado") != "cancelado" for p in (res.data or []))
    except Exception:
        traceback.print_exc()
        return False

def validar_codigo_descuento(codigo_texto, rest_key, numero=None):
    """Valida que un código se pueda usar ahora mismo en este restaurante.
    Devuelve (fila, None) si es válido, o (None, mensaje_error) si no."""
    fila = buscar_codigo_descuento(codigo_texto)
    if not fila:
        return None, "No encontramos ese código."
    if not fila.get("activo", True):
        return None, "Ese código ya no está disponible."
    if (fila.get("usos_restantes") or 0) <= 0:
        return None, "Ese código ya se agotó."
    fecha_exp = fila.get("fecha_expiracion")
    if fecha_exp:
        try:
            exp = datetime.fromisoformat(str(fecha_exp).replace("Z", "+00:00"))
            ahora = datetime.now(exp.tzinfo) if exp.tzinfo else datetime.now(ZONA_HORARIA).replace(tzinfo=None)
            if ahora > exp:
                return None, "Ese código ya venció."
        except Exception:
            pass
    # restaurante_id vacío/null = código de Caza Delivery, válido en cualquier restaurante
    if fila.get("restaurante_id") and fila["restaurante_id"] != rest_key:
        return None, "Ese código no aplica para este restaurante."
    if numero and cliente_ya_uso_codigo(numero, fila["codigo"]):
        return None, "Ya usaste este código antes — cada código solo se puede usar una vez por cliente."
    return fila, None

def restaurar_uso_codigo_si_aplica(pedido):
    """Si un pedido con código de descuento se cancela, se le devuelve el uso
    al código (no se debe perder un uso por un pedido que no se completó).
    El uso se había descontado ya al confirmarse el pedido (ver crear_pedido) —
    no al entregarse — precisamente para que dos clientes no puedan reservar
    a la vez el último uso de un código antes de que ninguno se entregue."""
    codigo_texto = pedido.get("codigo_descuento")
    if not codigo_texto:
        return
    fila = buscar_codigo_descuento(codigo_texto)
    if not fila:
        return
    try:
        totales = fila.get("usos_totales") or 0
        restantes = min((fila.get("usos_restantes") or 0) + 1, totales) if totales else (fila.get("usos_restantes") or 0) + 1
        datos = {"usos_restantes": restantes}
        if restantes > 0:
            datos["activo"] = True  # si se había agotado por este mismo pedido, vuelve a estar disponible
        supabase.table("codigos_descuento").update(datos).eq("id", fila["id"]).execute()
    except Exception:
        traceback.print_exc()

def descripcion_descuento(fila):
    valor = fila.get("valor", 0)
    es_porcentaje = fila.get("tipo") == "porcentaje"
    if (fila.get("aplica_a") or "total") == "domicilio":
        if es_porcentaje:
            return "domicilio gratis" if valor >= 100 else f"{int(valor)}% de descuento en el domicilio"
        return f"${int(valor):,}".replace(",", ".") + " de descuento en el domicilio"
    if es_porcentaje:
        return f"{int(valor)}% de descuento"
    return f"${int(valor):,}".replace(",", ".") + " de descuento"

def consumir_uso_codigo(codigo_row):
    """Descuenta un uso del código; lo desactiva solo si ya no le quedan usos."""
    restantes = max((codigo_row.get("usos_restantes") or 1) - 1, 0)
    datos = {"usos_restantes": restantes}
    if restantes <= 0:
        datos["activo"] = False
    try:
        supabase.table("codigos_descuento").update(datos).eq("id", codigo_row["id"]).execute()
    except Exception:
        traceback.print_exc()

REENGANCHE_DIAS = 7
REENGANCHE_CODIGO = os.getenv("REENGANCHE_CODIGO", "").strip()

def enviar_reenganches_pendientes():
    """Ve pedidos entregados hace ~REENGANCHE_DIAS días y les manda a esos
    clientes un mensaje con un código de descuento para que vuelvan a pedir.
    No hace nada si no se configuró REENGANCHE_CODIGO (variable de entorno en
    Railway) — la función queda apagada por defecto hasta que el admin cree
    ese código desde el panel y configure la variable."""
    if not REENGANCHE_CODIGO:
        return
    try:
        objetivo = datetime.now(ZONA_HORARIA) - timedelta(days=REENGANCHE_DIAS)
        desde = (objetivo - timedelta(hours=6)).isoformat()
        hasta = (objetivo + timedelta(hours=6)).isoformat()
        res = supabase.table("pedidos").select("*")\
            .eq("estado", "entregado").gte("fecha", desde).lte("fecha", hasta).execute()
        pedidos = res.data or []
    except Exception:
        traceback.print_exc()
        return
    ya_procesados = set()
    for p in pedidos:
        numero = p.get("numero_cliente")
        if not numero or numero in ya_procesados:
            continue
        ya_procesados.add(numero)
        cli = get_cliente(numero)
        if not cli:
            continue
        # No reenviar si ya se le mandó uno hace menos de 30 días (aunque tenga
        # varios pedidos entregados en la ventana de esta corrida).
        ultimo = cli.get("ultimo_reenganche_enviado")
        if ultimo:
            try:
                ultimo_dt = datetime.fromisoformat(str(ultimo).replace("Z", "+00:00"))
                if ultimo_dt.tzinfo is None:
                    ultimo_dt = pytz.utc.localize(ultimo_dt)
                if (datetime.now(ZONA_HORARIA) - ultimo_dt.astimezone(ZONA_HORARIA)).days < 30:
                    continue
            except Exception:
                pass
        # validar_codigo_descuento también revisa que el código siga activo,
        # no esté vencido, aplique a este restaurante y que el cliente no lo
        # haya usado ya — si algo de eso falla, no tiene sentido avisarle.
        fila_codigo, _error = validar_codigo_descuento(REENGANCHE_CODIGO, p.get("restaurante_id", ""), numero)
        if not fila_codigo:
            continue
        try:
            enviar_whatsapp(numero,
                f"¡Hola {cli.get('nombre','')}! 👋 ¿Qué tal estuvo tu último pedido?\n"
                f"Como agradecimiento, usa el código *{REENGANCHE_CODIGO}* en tu próximo pedido: "
                f"{descripcion_descuento(fila_codigo)}. ¡Te esperamos! 😊"
            )
            actualizar_cliente(numero, {"ultimo_reenganche_enviado": datetime.now(ZONA_HORARIA).isoformat()})
        except Exception:
            traceback.print_exc()

async def tarea_reenganche_clientes():
    """Corre en segundo plano durante toda la vida del proceso: revisa cada 6
    horas si hay clientes elegibles para el mensaje de reenganche."""
    while True:
        try:
            enviar_reenganches_pendientes()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(6 * 60 * 60)

@app.on_event("startup")
async def iniciar_tareas_programadas():
    asyncio.create_task(tarea_reenganche_clientes())

def lista_restaurantes():
    lineas = ["🍽️ *Bienvenido a Ipiales Delivery*\n\nElige un restaurante:\n"]
    for i, (key, r) in enumerate(_cache_restaurantes.items(), 1):
        estado = "✅ Abierto" if esta_abierto(key) else "❌ Cerrado"
        lineas.append(f"{i}. *{r['nombre']}* — {estado}\n   📍 {r['direccion']}")
    lineas.append("\nResponde con el *número* o el *nombre* del restaurante.")
    return "\n".join(lineas)

def build_system_prompt(rest_key, cliente=None, descuento=None):
    r = get_restaurante(rest_key)
    extra = _estado_extra.get(rest_key, {})
    items = get_menu(rest_key)
    desact = extra.get("categorias_desactivadas", set())
    menu_activo_items = [i for i in items if i["activo"] and i["categoria"] not in desact]
    menu_activo = []
    for i in menu_activo_items:
        linea = i["descripcion"]
        agotados = i.get("productos_agotados") or []
        if agotados:
            linea += f" (NO disponible hoy: {', '.join(agotados)})"
        menu_activo.append(linea)
    hay_bebidas = any("bebida" in normalizar_texto(i["categoria"]) for i in menu_activo_items)
    notas = ("\nNOTAS DE HOY:\n- " + "\n- ".join(extra["notas"])) if extra.get("notas") else ""
    espera = f"\nTIEMPO DE ESPERA: {extra['tiempo_espera']} minutos." if extra.get("tiempo_espera") else ""
    costo_dom_normal = costo_domicilio(r)
    dom = f"Sí. Costo: ${costo_dom_normal:,}.".replace(",", ".") if extra.get("domicilio_activo", True) else "No disponible."

    saludo = ""
    instr_direccion_guardada = (
        "- Si el pedido es a domicilio, PREGÚNTALE primero si quiere que se lo enviemos a su dirección "
        "guardada o si prefiere dar una diferente. NO la uses automáticamente sin que el cliente confirme cuál usar."
    )
    if cliente:
        dir_guardada = (cliente.get("direccion") or "").strip()
        if dir_guardada:
            saludo = f"\nEl cliente se llama *{cliente['nombre']}* y su dirección habitual es *{dir_guardada}*. Salúdalo por su nombre."
            instr_direccion_guardada = (
                f"- Si el cliente pide domicilio, PREGÚNTALE si quiere que se lo enviemos a su dirección guardada "
                f"(*{dir_guardada}*) o si prefiere dar una diferente. NO la uses automáticamente sin que confirme cuál usar."
            )
        else:
            saludo = f"\nEl cliente se llama *{cliente['nombre']}*. Salúdalo por su nombre."

    upsell = (
        '\n- Si al momento de cerrar el pedido el cliente no ha pedido ninguna bebida, sugiérele UNA sola vez '
        'agregar algo de tomar (ej: "¿quieres agregar algo de tomar? 🥤") antes de mostrar el resumen final. '
        'Si dice que no, respeta su decisión y no insistas de nuevo con eso.'
    ) if hay_bebidas else ""

    metodos_pago = metodos_pago_texto(r)

    descuento_txt = ""
    if descuento:
        monto_min = descuento.get("monto_minimo") or 0
        monto_min_txt = f"{monto_min:,.0f}".replace(",", ".")
        aplica_a = descuento.get("aplica_a") or "total"
        condicion_txt = f" — SOLO aplica si el subtotal es de al menos ${monto_min_txt}" if monto_min > 0 else ""
        descuento_txt = f"\nDESCUENTO ACTIVO: {descripcion_descuento(descuento)} (código {descuento['codigo']}){condicion_txt}."

        condicion_minimo = (
            f"Este código SOLO aplica si el subtotal del pedido (antes del descuento) es de al menos ${monto_min_txt}. "
            f"Si el pedido no llega a ese monto, avísale amablemente que le falta para poder usar el código, NO "
            f"apliques ningún descuento, y deja que continúe su pedido normal si así lo prefiere. Si sí llega al "
            f"mínimo: "
        ) if monto_min > 0 else ""

        if aplica_a == "domicilio":
            # El costo de domicilio NO depende del carrito (es fijo por restaurante), así que
            # el valor ya-con-descuento lo calculamos aquí mismo en vez de pedirle a Claude que
            # haga la resta/porcentaje — así nunca se le "pasa" el cálculo. Lo que si depende
            # del carrito es si se cumple el mínimo de compra, así que dejamos AMBOS números
            # (normal y con descuento) en la misma línea DOMICILIO de arriba, para que Claude
            # solo tenga que comparar el subtotal contra el mínimo y copiar el número que
            # corresponda — nunca calcular nada, y nunca ver un solo número que contradiga esto.
            valor_desc = descuento.get("valor", 0) or 0
            if descuento.get("tipo") == "porcentaje":
                monto_descuento_dom = costo_dom_normal * (valor_desc / 100)
            else:
                monto_descuento_dom = valor_desc
            monto_descuento_dom = min(monto_descuento_dom, costo_dom_normal)
            domicilio_final = max(costo_dom_normal - monto_descuento_dom, 0)
            domicilio_final_txt = f"{domicilio_final:,.0f}".replace(",", ".")
            costo_dom_normal_txt = f"{costo_dom_normal:,.0f}".replace(",", ".")
            if monto_min > 0:
                dom = (
                    f"Sí. Costo normal: ${costo_dom_normal_txt}. Con el código {descuento['codigo']} aplicado, SI el "
                    f"subtotal es de al menos ${monto_min_txt}: ${domicilio_final_txt} exacto (no calcules el "
                    f"descuento, solo compara el subtotal contra el mínimo y usa el número que corresponda)."
                )
            else:
                dom = (
                    f"Sí. Costo: ${domicilio_final_txt} exacto (código {descuento['codigo']} aplicado — el domicilio "
                    f"normal de ${costo_dom_normal_txt} ya quedó rebajado, no hagas tú esa cuenta, solo usa este número)."
                )
            instr_aplicacion = (
                f"El costo de domicilio para ESTE pedido ya está resuelto arriba en DOMICILIO — úsalo tal cual, sin "
                f"calcular nada. Si el pedido es para recoger en el local (no es domicilio), este descuento no "
                f"aplica: avísale al cliente que este código solo sirve para pedidos a domicilio."
            )
        else:
            instr_aplicacion = f"{condicion_minimo}Descuéntalo del total del pedido."

        instr_descuento = (
            f"\n- El cliente ya tiene un código de descuento validado (ver DESCUENTO ACTIVO arriba). {instr_aplicacion} "
            f"Antes de mostrar el resumen final, muestra el subtotal, el descuento aplicado y el total ya con el "
            f"descuento (o el costo de domicilio ya rebajado, según corresponda)."
        )
    else:
        instr_descuento = (
            "\n- Antes de mostrar el resumen final, pregúntale al cliente si tiene algún código de descuento o "
            "promocional (ej: \"¿Tienes algún código de descuento? Si tienes uno, escríbeme: código TUCODIGO\"). "
            "Si dice que no tiene, sigue normal sin insistir más."
        )

    return f"""Eres el asistente virtual de *{r['nombre']}*, en {r['direccion']}, Ipiales.
HORARIO: {formato_horario(r)}
DOMICILIO: {dom}
MÉTODOS DE PAGO: {metodos_pago}.{descuento_txt}
MENÚ:
{chr(10).join(menu_activo)}
{notas}{espera}{saludo}

INSTRUCCIONES:
- Habla amigable y natural como empleado real de {r['nombre']}.
{instr_direccion_guardada}
- Acumula todos los productos sin mostrar resumen parcial.
- Si el cliente pide algo ambiguo (falta tamaño, sabor, o hay varias opciones parecidas en el menú), pregunta cuál quiere exactamente antes de agregarlo al pedido — no asumas ni adivines.
- Confirma la cantidad exacta de cada producto a medida que el cliente lo va pidiendo.
- NUNCA agregues al pedido un producto que no esté escrito tal cual en el MENÚ de arriba.
- Si un producto aparece marcado como "(NO disponible hoy: ...)", NO lo ofrezcas ni lo agregues al pedido aunque el cliente lo pida — avísale amablemente que hoy no hay y sugiérele otra opción del menú.
- NUNCA muestres resumen ni total hasta que el cliente diga "es todo", "listo", "eso sería" o similar.{upsell}
- Solo entonces muestra resumen completo con total.
- Si el cliente mencionó lugar de entrega, es domicilio. Confirma la dirección.
- Si el pedido es a domicilio y NO tienes clara la dirección, dile textualmente algo como: "Para el domicilio, escríbeme tu dirección completa (barrio, calle/carrera y número), o si prefieres, envíame tu ubicación actual desde WhatsApp (toca el clip 📎 → Ubicación)." No cierres el pedido a domicilio sin dirección confirmada por ninguna de esas dos vías.
- En el resumen final de un pedido a domicilio SIEMPRE detalla los datos de entrega: dirección exacta (o la ubicación que envió) y el nombre de quien recibe.
- Antes de mostrar el resumen final, pregúntale al cliente cómo va a pagar (elige una de las opciones en MÉTODOS DE PAGO de arriba). Si elige Nequi o Bre-B, dale el número EXACTO que aparece en MÉTODOS DE PAGO y pídele que envíe la foto del comprobante de la transferencia aquí por WhatsApp. Incluye el método de pago elegido en el resumen final.{instr_descuento}
- Al confirmar el pedido SIEMPRE termina con una frase EXACTA en una línea separada, según el método de pago elegido:
  - Si paga en EFECTIVO: "✅ Pedido recibido. Estamos preparando tu pedido 🍔"
  - Si paga por NEQUI o BRE-B: "✅ Pedido recibido. Apenas confirmes tu pago (envía la captura aquí) el restaurante lo revisará y ahí sí empezaremos a preparar tu pedido 🍔"
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
            texto_item = item["descripcion"]
            agotados = item.get("productos_agotados") or []
            if agotados:
                texto_item += f"\n_(No disponible hoy: {', '.join(agotados)})_"
            lineas.append(texto_item)
    if extra.get("notas"):
        lineas.append("\n📝 *Notas de hoy:*")
        for nota in extra["notas"]:
            lineas.append(f"- {nota}")
    dom = f"Sí, costo ${costo_domicilio(r):,}".replace(",", ".") if extra.get("domicilio_activo", True) else "No disponible"
    lineas.append(f"\n🛵 *Domicilio:* {dom}")
    lineas.append(f"💳 *Pago:* {metodos_pago_texto(r)}")
    enviar_whatsapp(numero, "\n\n".join(lineas))

def enviar_menu_segun_modo(numero, rest_key):
    """Manda el menú en el formato que el restaurante configuró:
    PDF adjunto, link a su página, o el texto automático como respaldo."""
    r = get_restaurante(rest_key)
    modo = r.get("menu_modo") if r else None
    url_menu = r.get("menu_url") if r else None
    if modo == "pdf" and url_menu:
        enviar_whatsapp_documento(numero, url_menu, f"Menu-{r['nombre']}.pdf", f"📋 Menú de {r['nombre']}")
    elif modo == "link" and url_menu:
        enviar_whatsapp(numero, f"📋 Puedes ver el menú completo de *{r['nombre']}* aquí:\n{url_menu}")
    else:
        enviar_menu_texto(numero, rest_key)

# ── NOTIFICACIONES ADMIN ──────────────────────────────────────────────────────

def notificar_pedido_admin(numero, pedido, es_nuevo=True):
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    prefijo = "🛎️ *Pedido nuevo*" if es_nuevo else "🔄 *Pedido actualizado*"
    cli = get_cliente(numero)
    nombre_cliente = cli.get("nombre", "") if cli else ""
    msg = (
        f"{prefijo} #{pedido['id']}\n"
        f"🍽️ {pedido.get('restaurante_nombre', '')}\n"
        f"👤 {nombre_cliente or 'Sin nombre'}\n"
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

def notificar_restaurante(r, mensaje):
    """Manda un mensaje al WhatsApp de notificación del restaurante. Soporta
    varios números separados por coma (ej. dueño y encargado) — les manda el
    mismo mensaje a todos."""
    if not r:
        return
    numeros = [n.strip() for n in (r.get("whatsapp_notificacion") or "").split(",") if n.strip()]
    for numero in numeros:
        enviar_whatsapp(numero, mensaje)

def notificar_pedido_restaurante(pedido, rest_key, es_nuevo=True):
    """Manda el aviso de pedido nuevo/actualizado directo al WhatsApp del
    restaurante, si tiene uno o más números configurados para eso."""
    r = get_restaurante(rest_key)
    if not r or not (r.get("whatsapp_notificacion") or "").strip():
        return
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    prefijo = "🛎️ *Pedido nuevo*" if es_nuevo else "🔄 *Pedido actualizado*"
    cli = get_cliente(pedido.get("numero_cliente", ""))
    nombre_cliente = cli.get("nombre", "") if cli else ""
    msg = (
        f"{prefijo} #{pedido['id']}\n"
        f"👤 {nombre_cliente or 'Sin nombre'}\n"
        f"🕐 {pedido['hora']}\n"
        f"{icono} {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger'}\n"
        f"📍 {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}\n"
        f"────────────────\n"
        f"👉 Entra a tu panel: {os.getenv('PANEL_URL', '')}/panel-restaurante"
    )
    notificar_restaurante(r, msg)

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

def normalizar_telefono(t):
    """Deja solo los dígitos del teléfono (quita +, espacios, guiones, paréntesis)."""
    return re.sub(r"\D", "", str(t or ""))

def get_domiciliario_by_telefono(telefono):
    """Busca comparando los últimos 10 dígitos (la línea en Colombia), para que
    funcione sin importar si el teléfono se guardó con +57, espacios, guiones o
    sin indicativo — WhatsApp siempre manda 57XXXXXXXXXX pero el admin puede
    haberlo escrito de cualquier forma."""
    objetivo = normalizar_telefono(telefono)[-10:]
    if not objetivo:
        return None
    try:
        res = supabase.table("domiciliarios").select("*").execute()
        for dom in res.data or []:
            if normalizar_telefono(dom.get("telefono"))[-10:] == objetivo:
                return dom
        return None
    except Exception:
        return None

def get_restaurante_por_numero_notificacion(numero):
    """Si este número está configurado como número de aviso de pedidos de
    algún restaurante (whatsapp_notificacion admite varios separados por
    coma), devuelve ese restaurante. None si no es de ninguno — así el bot
    puede avisarle que use su panel en vez de dejarlo pedir comida por error."""
    objetivo = normalizar_telefono(numero)[-10:]
    if not objetivo:
        return None
    for r in _cache_restaurantes.values():
        numeros = (r.get("whatsapp_notificacion") or "").split(",")
        for n in numeros:
            if normalizar_telefono(n)[-10:] == objetivo:
                return r
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

def set_disponible_domiciliario(dom_id, disponible):
    try:
        supabase.table("domiciliarios").update({"disponible": disponible}).eq("id", dom_id).execute()
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


def _ip_cliente(request):
    """IP real del que hace la petición, prefiriendo X-Forwarded-For (Railway
    corre detrás de un proxy, así que request.client.host solo sería el proxy)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "desconocido"

def login_bloqueado(request, tipo):
    """Revisa si esta IP está bloqueada para este tipo de login por demasiados
    intentos fallidos recientes. Devuelve (bloqueado, segundos_restantes)."""
    clave = f"{tipo}:{_ip_cliente(request)}"
    ahora = time.time()
    hasta = _bloqueos_login.get(clave)
    if hasta and ahora < hasta:
        return True, int(hasta - ahora)
    if hasta:
        del _bloqueos_login[clave]
    return False, 0

def registrar_intento_fallido(request, tipo):
    clave = f"{tipo}:{_ip_cliente(request)}"
    ahora = time.time()
    intentos = [t for t in _intentos_login.get(clave, []) if ahora - t < INTENTOS_LOGIN_VENTANA_SEG]
    intentos.append(ahora)
    _intentos_login[clave] = intentos
    if len(intentos) >= INTENTOS_LOGIN_MAX:
        _bloqueos_login[clave] = ahora + INTENTOS_LOGIN_BLOQUEO_SEG
        _intentos_login[clave] = []

def registrar_intento_exitoso(request, tipo):
    clave = f"{tipo}:{_ip_cliente(request)}"
    _intentos_login.pop(clave, None)

def procesar_mensaje_domiciliario(numero, texto):
    """Maneja mensajes de domiciliarios (entro turno / salgo turno).
    Retorna respuesta si el número es de un domiciliario, None si no."""
    dom = get_domiciliario_by_telefono(numero)
    if not dom:
        return None

    t = texto.strip().lower()

    if any(p in t for p in ["entro turno", "entro al turno", "inicio turno", "empiezo turno", "estoy disponible", "ya estoy"]):
        set_disponible_domiciliario(dom["id"], True)
        return (
            f"✅ *¡Listo {dom['nombre']}!* Estás en turno.\n\n"
            f"Te avisaremos cuando haya un pedido disponible. 🛵\n\n"
            f"Escribe *salgo turno* cuando termines."
        )

    if any(p in t for p in ["salgo turno", "salgo del turno", "termino turno", "fin turno", "ya no estoy", "me voy"]):
        set_disponible_domiciliario(dom["id"], False)
        return f"👋 *Hasta luego {dom['nombre']}!* Quedas fuera de turno.\n\nEscribe *entro turno* cuando vuelvas a estar disponible."

    if any(p in t for p in ["ayuda", "help", "comandos"]):
        return (
            f"🛵 *Comandos disponibles {dom['nombre']}:*\n\n"
            f"• *entro turno* → activarte para recibir pedidos\n"
            f"• *salgo turno* → desactivarte\n\n"
            f"Estado actual: {'✅ En turno' if dom.get('disponible') else '❌ Fuera de turno'}\n\n"
            f"También puedes pedir comida normalmente (ej. \"restaurantes\") con este mismo número 😊"
        )

    # No es un comando de turno conocido: puede ser que el domiciliario quiera
    # pedir comida para él mismo, así que se deja pasar al flujo normal de cliente
    # (elegir restaurante, menú, Claude) en vez de responder siempre lo mismo.
    return None

def pedido_ya_asignado(pedido_id):
    try:
        res = supabase.table("asignaciones").select("*").eq("pedido_id", pedido_id).execute()
        return len(res.data) > 0
    except Exception:
        return False

def pedido_asignado_a(pedido_id, dom_id):
    """Verifica que este pedido esté realmente asignado a este domiciliario específico."""
    try:
        res = supabase.table("asignaciones").select("*").eq("pedido_id", pedido_id).eq("domiciliario_id", dom_id).execute()
        return len(res.data) > 0
    except Exception:
        return False

def get_pedidos_sin_asignar():
    """Pedidos de domicilio que están esperando domiciliario, directo desde la BD.
    (Antes esto vivía en un dict en memoria que se borraba con cada reinicio de
    Railway — por eso a veces los domiciliarios no veían nada en la app.)
    Solo cuentan los que el restaurante ya marcó con "busqueda_domiciliario" (al
    presionar "Buscar domiciliario") — si no, CUALQUIER pedido nuevo quedaría
    disponible para que cualquier domiciliario lo acepte de inmediato, sin que
    el restaurante llegue a revisarlo ni a decidir buscar uno."""
    try:
        res = supabase.table("pedidos").select("*")\
            .eq("tipo", "domicilio")\
            .eq("busqueda_domiciliario", True)\
            .in_("estado", ["activo", "preparando"])\
            .order("fecha", desc=False)\
            .limit(20).execute()
        candidatos = res.data or []
        if not candidatos:
            return []
        ids = [p["id"] for p in candidatos]
        asig = supabase.table("asignaciones").select("pedido_id").in_("pedido_id", ids).execute()
        asignados = {a["pedido_id"] for a in (asig.data or [])}
        return [p for p in candidatos if p["id"] not in asignados]
    except Exception:
        traceback.print_exc()
        return []

def notificar_domiciliarios_whatsapp(pedido):
    """Notifica a domiciliarios disponibles por WhatsApp como respaldo"""
    doms = get_domiciliarios_disponibles()
    if not doms:
        return
    cli = get_cliente(pedido.get("numero_cliente", ""))
    nombre_cliente = cli.get("nombre", "") if cli else ""
    r = get_restaurante(pedido.get("restaurante_id", ""))
    ganancia = f"${costo_domicilio(r):,}".replace(",", ".")
    for dom in doms:
        msg = (
            f"🛵 *¡Pedido nuevo #{pedido['id']}!*\n"
            f"🍽️ {pedido.get('restaurante_nombre', '')}\n"
            f"👤 Cliente: {nombre_cliente or 'Sin nombre'}\n"
            f"📍 Dirección: {pedido.get('direccion', '')}\n"
            f"────────────────\n"
            f"{pedido.get('resumen', '')}\n"
            f"────────────────\n"
            f"💰 Ganancia: {ganancia}\n\n"
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
<title>Panel general — Caza Delivery</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#fafafa;--surface:#ffffff;--border:#efefef;--text:#2a2a2a;--text2:#999999;
  --accent1:#667eea;--accent2:#764ba2;--accent-ring:rgba(102,126,234,.18);--red:#d32f2f;
}
:root[data-theme="dark"]{
  --bg:#12151a;--surface:#1a1e24;--border:#2a2f37;--text:#e8e8e8;--text2:#7a8088;--red:#f28b8b;
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme="light"]):not([data-theme="dark"]){
    --bg:#12151a;--surface:#1a1e24;--border:#2a2f37;--text:#e8e8e8;--text2:#7a8088;--red:#f28b8b;
  }
}
body{background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text);transition:background .2s ease,color .2s ease}
.box{background:var(--surface);border:1px solid var(--border);box-shadow:0 8px 28px rgba(0,0,0,.08);padding:32px 28px;border-radius:16px;text-align:center;width:90%;max-width:340px}
.logo{width:48px;height:48px;margin:0 auto 14px;background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;color:#fff}
h1{font-size:1.2rem;margin-bottom:4px;font-weight:700}
h1 span{background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
p{color:var(--text2);margin-bottom:20px;font-size:.87rem}
input{width:100%;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:10px;font-size:1rem;outline:none;margin-bottom:12px;color:var(--text);font-family:inherit}
input:focus{border-color:var(--accent1);box-shadow:0 0 0 3px var(--accent-ring)}
button{width:100%;padding:12px;background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);border:none;border-radius:10px;color:#fff;font-weight:700;font-size:1rem;cursor:pointer;transition:filter .15s ease}
button:hover{filter:brightness(1.08)}
.err{color:var(--red);font-size:.82rem;margin:-6px 0 12px;display:none}
.theme-toggle{margin-top:16px;background:none;border:none;color:var(--text2);font-size:.78rem;cursor:pointer;width:auto;padding:6px}
.theme-toggle:hover{color:var(--text)}
</style></head><body>
<div class="box">
  <div class="logo">P</div>
  <h1>Panel <span>General</span></h1>
  <p>Caza Delivery</p>
  <form onsubmit="entrar(event)">
    <input type="password" id="pw" placeholder="Contraseña" autofocus>
    <div class="err" id="err">Contraseña incorrecta</div>
    <button type="submit">Entrar</button>
  </form>
  <button class="theme-toggle" onclick="toggleTemaLogin()" id="theme-toggle-btn"><span id="theme-toggle-icon">☾</span> <span id="theme-toggle-label">Modo oscuro</span></button>
</div>
<script>
function aplicarTemaLogin(tema) {
  document.documentElement.setAttribute("data-theme", tema);
  document.getElementById("theme-toggle-icon").textContent = tema === "dark" ? "☀" : "☾";
  document.getElementById("theme-toggle-label").textContent = tema === "dark" ? "Modo claro" : "Modo oscuro";
}
function toggleTemaLogin() {
  const actual = document.documentElement.getAttribute("data-theme");
  const nuevo = actual === "dark" ? "light" : "dark";
  localStorage.setItem("panel-general-theme", nuevo);
  aplicarTemaLogin(nuevo);
}
(function initTemaLogin() {
  const guardado = localStorage.getItem("panel-general-theme");
  const tema = guardado || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  aplicarTemaLogin(tema);
})();
if (new URLSearchParams(window.location.search).get('pw')) {
  document.getElementById('err').style.display = 'block';
}
function entrar(e){e.preventDefault();window.location.href='/panel?pw='+encodeURIComponent(document.getElementById('pw').value);}
</script>
</body></html>"""

LOGIN_RESTAURANTE_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel del restaurante — Ipiales Delivery</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#fafafa;--surface:#ffffff;--border:#efefef;--text:#2a2a2a;--text2:#999999;
  --accent1:#667eea;--accent2:#764ba2;--accent-ring:rgba(102,126,234,.18);
  --red:#d32f2f;
}
:root[data-theme="dark"]{
  --bg:#12151a;--surface:#1a1e24;--border:#2a2f37;--text:#e8e8e8;--text2:#7a8088;--red:#f28b8b;
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme="light"]):not([data-theme="dark"]){
    --bg:#12151a;--surface:#1a1e24;--border:#2a2f37;--text:#e8e8e8;--text2:#7a8088;--red:#f28b8b;
  }
}
body{background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text);transition:background .2s ease,color .2s ease}
.box{background:var(--surface);border:1px solid var(--border);box-shadow:0 8px 28px rgba(0,0,0,.08);padding:32px 28px;border-radius:16px;text-align:center;width:90%;max-width:340px;position:relative}
.logo{width:48px;height:48px;margin:0 auto 14px;background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;color:#fff}
h1{font-size:1.2rem;margin-bottom:4px;font-weight:700}
h1 span{background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
p{color:var(--text2);margin-bottom:20px;font-size:.87rem}
input{width:100%;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:10px;font-size:1rem;outline:none;margin-bottom:12px;color:var(--text);font-family:inherit}
input:focus{border-color:var(--accent1);box-shadow:0 0 0 3px var(--accent-ring)}
button{width:100%;padding:12px;background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);border:none;border-radius:10px;color:#fff;font-weight:700;font-size:1rem;cursor:pointer;transition:filter .15s ease}
button:hover{filter:brightness(1.08)}
.theme-toggle{margin-top:16px;background:none;border:none;color:var(--text2);font-size:.78rem;cursor:pointer;width:auto;padding:6px}
.theme-toggle:hover{color:var(--text)}
</style></head><body>
<div class="box">
  <div class="logo">R</div>
  <h1>Panel de <span>tu restaurante</span></h1>
  <p>Ipiales Delivery</p>
  <form id="form-login">
    <input type="password" id="pw" placeholder="Contraseña de tu restaurante" autofocus>
    <div class="err" id="err" style="color:var(--red);font-size:.82rem;margin:-6px 0 12px;display:none">Contraseña incorrecta</div>
    <button type="submit">Entrar</button>
  </form>
  <button class="theme-toggle" onclick="toggleTemaLogin()" id="theme-toggle-btn"><span id="theme-toggle-icon">☾</span> <span id="theme-toggle-label">Modo oscuro</span></button>
</div>
<script>
function aplicarTemaLogin(tema) {
  document.documentElement.setAttribute("data-theme", tema);
  document.getElementById("theme-toggle-icon").textContent = tema === "dark" ? "☀" : "☾";
  document.getElementById("theme-toggle-label").textContent = tema === "dark" ? "Modo claro" : "Modo oscuro";
}
function toggleTemaLogin() {
  const actual = document.documentElement.getAttribute("data-theme");
  const nuevo = actual === "dark" ? "light" : "dark";
  localStorage.setItem("panel-restaurante-theme", nuevo);
  aplicarTemaLogin(nuevo);
}
(function initTemaLogin() {
  const guardado = localStorage.getItem("panel-restaurante-theme");
  const tema = guardado || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  aplicarTemaLogin(tema);
})();

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
<title>Panel general — Caza Delivery</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#fafafa;--surface:#ffffff;--surface2:#f5f5f5;--border:#efefef;--border2:#f5f5f5;
  --text:#2a2a2a;--text2:#666666;--text3:#999999;
  --accent1:#667eea;--accent2:#764ba2;--accent-shadow:rgba(102,126,234,.25);--accent-ring:rgba(102,126,234,.1);
  --red:#d32f2f;--red-light:#ffebee;--red-dark:#c62828;
  --green:#2e7d32;--green-light:#e8f5e9;
  --blue:#1565c0;--blue-light:#e3f2fd;
  --purple:#6a1b9a;--purple-light:#f3e5f5;
  --yellow:#e65100;--yellow-light:#fff3e0;
  --shadow-sm:0 1px 3px rgba(0,0,0,.04);
  --shadow-md:0 4px 12px rgba(0,0,0,.08);
}
:root[data-theme="dark"]{
  --bg:#12151a;--surface:#1a1e24;--surface2:#20252c;--border:#2a2f37;--border2:#262b32;
  --text:#e8e8e8;--text2:#a8adb5;--text3:#7a8088;
  --red:#f28b8b;--red-light:rgba(211,47,47,.18);--red-dark:#ff8a80;
  --green:#66bb6a;--green-light:rgba(46,125,50,.2);
  --blue:#64b5f6;--blue-light:rgba(21,101,192,.22);
  --purple:#ba68c8;--purple-light:rgba(106,27,154,.22);
  --yellow:#ffb74d;--yellow-light:rgba(230,81,0,.18);
  --shadow-sm:0 1px 3px rgba(0,0,0,.3);--shadow-md:0 4px 12px rgba(0,0,0,.45);
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme="light"]):not([data-theme="dark"]){
    --bg:#12151a;--surface:#1a1e24;--surface2:#20252c;--border:#2a2f37;--border2:#262b32;
    --text:#e8e8e8;--text2:#a8adb5;--text3:#7a8088;
    --red:#f28b8b;--red-light:rgba(211,47,47,.18);--red-dark:#ff8a80;
    --green:#66bb6a;--green-light:rgba(46,125,50,.2);
    --blue:#64b5f6;--blue-light:rgba(21,101,192,.22);
    --purple:#ba68c8;--purple-light:rgba(106,27,154,.22);
    --yellow:#ffb74d;--yellow-light:rgba(230,81,0,.18);
    --shadow-sm:0 1px 3px rgba(0,0,0,.3);--shadow-md:0 4px 12px rgba(0,0,0,.45);
  }
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;transition:background .2s ease,color .2s ease}
.layout{display:flex;min-height:100vh}
.sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:22px 0;position:fixed;top:0;left:0;height:100vh;display:flex;flex-direction:column;z-index:100;box-shadow:var(--shadow-sm)}
.sidebar-logo{padding:0 22px 20px;border-bottom:1px solid var(--border2);margin-bottom:14px}
.sidebar-logo h1{font-size:1.1rem;color:var(--accent1);font-weight:700;letter-spacing:-.3px}
.sidebar-logo p{font-size:.72rem;color:var(--text3);margin-top:3px}
.nav-item{display:flex;align-items:center;gap:11px;padding:12px 22px;cursor:pointer;color:var(--text2);font-size:.88rem;font-weight:500;transition:all .18s ease;border-left:3px solid transparent}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:rgba(102,126,234,.1);color:var(--accent1);border-left-color:var(--accent1)}
.nav-item .icon{font-size:1.1rem;width:20px;text-align:center}
.sidebar-footer{padding:16px 20px;border-top:1px solid var(--border2);display:flex;flex-direction:column;gap:10px;margin-top:auto}
.theme-toggle{display:flex;align-items:center;gap:8px;width:100%;padding:9px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--text2);font-size:.78rem;font-weight:600;cursor:pointer}
.theme-toggle:hover{color:var(--text);border-color:var(--accent1)}
.main{margin-left:230px;padding:28px;flex:1}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}
.topbar h2{font-size:1.3rem;font-weight:700}
.btn{padding:9px 16px;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:.82rem;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);color:#fff}
.btn-primary:hover{filter:brightness(1.08)}
.btn-ghost{background:var(--surface2);color:var(--text2);border:1px solid var(--border)}
.btn-ghost:hover{color:var(--text);border-color:var(--accent1)}
.stats{display:flex;gap:20px;margin-bottom:20px;font-size:.82rem;color:var(--text2);font-weight:600;flex-wrap:wrap}
.cards-resumen{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
.card-r{background:var(--surface);border-radius:12px;padding:16px;text-align:center;border:1px solid var(--border2);box-shadow:var(--shadow-sm)}
.card-r .num{display:block;font-size:1.6rem;font-weight:700;color:var(--text)}
.card-r .lbl{display:block;font-size:.72rem;color:var(--text3);margin-top:4px}
.card-r.total .num{background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;font-size:1.7rem}
.card-r.cancel .num{color:var(--red)}
.tabs{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap;border-bottom:1px solid var(--border2)}
.tab{background:none;color:var(--text3);border:none;border-bottom:2px solid transparent;padding:10px 4px;margin-right:16px;cursor:pointer;font-size:.85rem;font-weight:600;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.on{color:var(--accent1);border-bottom-color:var(--accent1)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--border2);border-left:3px solid var(--accent1);border-radius:12px;padding:16px;box-shadow:var(--shadow-sm)}
.pid{font-size:1.05rem;font-weight:700;color:var(--accent1)}
.rest-tag{display:inline-block;background:var(--yellow-light);color:var(--yellow);padding:3px 9px;border-radius:6px;font-size:.72rem;font-weight:700;margin:6px 0 2px}
.hora{font-size:.78rem;color:var(--text3)}
.tipo-tag{display:inline-block;background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:3px 9px;border-radius:6px;font-size:.72rem;font-weight:700;margin-top:6px}
.cli{font-size:.83rem;color:var(--text2);margin:8px 0 2px}
.cli-nombre{font-size:.83rem;color:var(--accent1);font-weight:700;margin:2px 0}
.dir{font-size:.78rem;color:var(--text3);margin-bottom:6px;word-break:break-word}
.resumen{background:var(--bg);padding:10px;border-radius:8px;font-size:.76rem;color:var(--text2);max-height:100px;overflow-y:auto;border-left:3px solid var(--accent1);white-space:pre-wrap;margin:8px 0}
.mods{background:var(--surface2);border-left:3px solid var(--accent1);padding:8px;border-radius:8px;margin:8px 0;font-size:.73rem;color:var(--text2)}
.quejas-box{background:var(--red-light);border-left:3px solid var(--red);padding:8px;border-radius:8px;margin:8px 0;font-size:.73rem;color:var(--red-dark)}
.est-lbl{text-align:center;font-size:.78rem;font-weight:700;padding:6px;border-radius:6px;margin:10px 0 6px;background:var(--surface2);color:var(--text2)}
.est-lbl.activo{background:var(--blue-light);color:var(--blue)}.est-lbl.preparando{color:var(--accent1)}
.est-lbl.enviado{background:var(--blue-light);color:var(--blue)}.est-lbl.entregado{background:var(--purple-light);color:var(--purple)}
.est-lbl.cancelado{background:var(--red-light);color:var(--red-dark)}
.ebts{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}
.btn-buscar-dom{width:100%;padding:12px;border:none;border-radius:8px;background:linear-gradient(135deg,var(--accent1) 0%,var(--accent2) 100%);color:#fff;font-weight:700;font-size:.85rem;cursor:pointer;margin:8px 0}
.btn-buscar-dom:hover{filter:brightness(1.08)}
.btn-buscar-dom:disabled{opacity:.6;cursor:default}
.dom-buscando{width:100%;padding:11px;border-radius:8px;background:var(--surface2);color:var(--accent1);border:1px solid var(--border);font-weight:700;font-size:.85rem;text-align:center;margin:8px 0}
.dom-asignado{width:100%;padding:11px;border-radius:8px;background:var(--green-light);color:var(--green);font-weight:700;font-size:.85rem;text-align:center;margin:8px 0}
.eb{padding:9px 0;border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:700;background:var(--bg);color:var(--text3);opacity:.7;transition:all .15s}
.eb:hover{opacity:1}.eb.on{opacity:1;border-color:transparent}
.eb-activo.on{background:var(--green);color:#fff}.eb-preparando.on{background:var(--accent1);color:#fff}
.eb-enviado.on{background:var(--blue);color:#fff}.eb-entregado.on{background:var(--purple);color:#fff}.eb-cancelado.on{background:var(--red);color:#fff}
.empty{text-align:center;padding:50px 20px;color:var(--text3);font-size:.9rem;background:var(--surface);border:1px solid var(--border2);border-radius:12px}
.rev-rest{background:var(--surface);border:1px solid var(--border2);border-radius:12px;padding:18px;margin-bottom:16px;box-shadow:var(--shadow-sm)}
.rev-rest-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--border2)}
.rev-rest-header h3{font-size:1rem;font-weight:700}
.rev-prom{font-size:1.1rem;font-weight:700;color:var(--yellow)}
.rev-item{padding:10px 0;border-bottom:1px solid var(--border2);font-size:.82rem}
.rev-item:last-child{border-bottom:none}
.rev-stars{color:var(--yellow);font-weight:700;margin-bottom:4px}
.rev-comment{color:var(--text2)}
.rev-meta{color:var(--text3);font-size:.72rem;margin-top:4px}
.rev-item-alert{border-left:3px solid var(--red);padding-left:10px;background:var(--red-light);border-radius:0 8px 8px 0}
.nav-badge{background:var(--red);color:#fff;font-size:.68rem;font-weight:700;padding:1px 6px;border-radius:10px;margin-left:auto}
.filtro-rest{padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:.82rem;font-family:inherit}
.chart-bars{display:flex;align-items:flex-end;gap:9px;height:180px;margin-top:14px;padding:16px;background:var(--surface);border:1px solid var(--border2);border-radius:12px;box-shadow:var(--shadow-sm)}
.bar-wrap{flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;justify-content:flex-end;height:100%}
.bar{width:100%;background:linear-gradient(to top,var(--accent1),var(--accent2));border-radius:5px 5px 0 0;min-height:4px}
.bar-lbl{font-size:.68rem;color:var(--text2);text-align:center;font-weight:500;max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-val{font-size:.72rem;color:var(--accent1);font-weight:700}
@media(max-width:768px){
  .layout{flex-direction:column}
  .sidebar{position:static;width:100%;height:auto;flex-direction:row;align-items:center;padding:10px;overflow-x:auto}
  .sidebar-logo,.sidebar-footer{display:none}
  .sidebar nav{display:flex;flex-direction:row;gap:4px}
  .nav-item{flex-direction:column;gap:3px;padding:8px 12px;border-left:none;border-bottom:3px solid transparent;white-space:nowrap;font-size:.7rem}
  .nav-item.active{border-left-color:transparent;border-bottom-color:var(--accent1)}
  .main{margin-left:0;padding:16px}
}
</style></head><body>
<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-logo"><h1>Panel general</h1><p>Caza Delivery</p></div>
    <nav>
      <div class="nav-item active" id="nav-dashboard" onclick="mostrarVista('dashboard')"><span class="icon">▭</span> Dashboard</div>
      <div class="nav-item" id="nav-pedidos" onclick="mostrarVista('pedidos')"><span class="icon">☰</span> Pedidos</div>
      <div class="nav-item" id="nav-resenas" onclick="mostrarVista('resenas')"><span class="icon">★</span> Reseñas <span id="badge-resenas-bajas" class="nav-badge" style="display:none"></span></div>
      <div class="nav-item" id="nav-analitica" onclick="mostrarVista('analitica')"><span class="icon">▢</span> Analítica</div>
    </nav>
    <div class="sidebar-footer">
      <button class="theme-toggle" onclick="toggleTheme()" id="theme-toggle-btn"><span id="theme-toggle-icon">☾</span> <span id="theme-toggle-label">Modo oscuro</span></button>
      <a href="/" style="color:var(--text3);font-size:.78rem;text-decoration:none">↗ Ver página pública</a>
      <a href="#" onclick="window.location.href='/panel';return false;" style="color:var(--text3);font-size:.78rem;text-decoration:none">⊗ Salir</a>
    </div>
  </aside>
  <main class="main">
    <div id="view-dashboard">
      <div class="topbar"><h2>Dashboard</h2><button class="btn btn-primary" onclick="cargarPedidos()">↻ Actualizar</button></div>
      <div class="stats"><span id="s-hora">--:--</span><span id="s-total">Total: 0</span><span id="s-activos">Activos: 0</span></div>
      <div class="cards-resumen">
        <div class="card-r"><span class="num" id="v-cant">0</span><span class="lbl">Pedidos hoy</span></div>
        <div class="card-r total"><span class="num" id="v-total">$0</span><span class="lbl">Total vendido hoy</span></div>
        <div class="card-r cancel"><span class="num" id="v-cancel">0</span><span class="lbl">Cancelados hoy</span></div>
      </div>
    </div>
    <div id="view-pedidos" style="display:none">
      <div class="topbar">
        <h2>Pedidos</h2>
        <div style="display:flex;gap:8px;align-items:center">
          <select class="filtro-rest" id="filtro-rest-pedidos" onchange="cambiarFiltroRestaurante(this.value)"><option value="">Todos los restaurantes</option></select>
          <button class="btn btn-ghost" onclick="exportarCSV(filtrar(tabActual))">⬇ Exportar CSV</button>
          <button class="btn btn-primary" onclick="cargarPedidos()">↻ Actualizar</button>
        </div>
      </div>
      <div class="tabs">
        <button class="tab on" data-tab="todos" onclick="cambiarTab('todos')">Todos <span id="c-todos"></span></button>
        <button class="tab" data-tab="preparacion" onclick="cambiarTab('preparacion')">Recibidos <span id="c-prep"></span></button>
        <button class="tab" data-tab="enviados" onclick="cambiarTab('enviados')">Enviados <span id="c-env"></span></button>
        <button class="tab" data-tab="entregados" onclick="cambiarTab('entregados')">Entregados <span id="c-ent"></span></button>
      </div>
      <div id="grid" class="grid"></div>
      <div id="empty" class="empty" style="display:none">No hay pedidos en esta pestaña</div>
    </div>
    <div id="view-resenas" style="display:none">
      <div class="topbar">
        <h2>Reseñas</h2>
        <select class="filtro-rest" id="filtro-rest-resenas" onchange="cambiarFiltroRestaurante(this.value)"><option value="">Todos los restaurantes</option></select>
      </div>
      <div id="resenas-lista"></div>
    </div>
    <div id="view-analitica" style="display:none">
      <div class="topbar"><h2>Analítica</h2></div>
      <div class="cards-resumen" id="analitica-resumen"></div>
      <h3 style="font-size:.9rem;color:var(--text2);margin:20px 0 6px">Ventas de hoy por restaurante</h3>
      <div class="chart-bars" id="chart-rest"></div>
    </div>
  </main>
</div>
<script>
const pw="{{PW}}";
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function exportarCSV(lista){
  if(!lista||!lista.length){alert('No hay pedidos para exportar');return;}
  const filas=[["ID","Fecha","Restaurante","Cliente","Tipo","Dirección","Productos","Subtotal","Descuento","Total","Pago","Estado"]];
  lista.forEach(p=>filas.push([
    p.id, p.fecha||'', p.restaurante_nombre||'', p.numero_cliente||'', p.tipo||'', p.direccion||'',
    p.productos||'', p.subtotal||0, p.descuento_monto||0, p.total||0, p.metodo_pago||'', p.estado||''
  ]));
  const csv=filas.map(f=>f.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
  const blob=new Blob(["﻿"+csv],{type:"text/csv;charset=utf-8"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download=`pedidos_${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
let todos=[],tabActual="todos",restauranteFiltro="";

// ── TEMA CLARO / OSCURO ──
function aplicarTema(tema){
  document.documentElement.setAttribute("data-theme",tema);
  document.getElementById("theme-toggle-icon").textContent=tema==="dark"?"☀":"☾";
  document.getElementById("theme-toggle-label").textContent=tema==="dark"?"Modo claro":"Modo oscuro";
}
function toggleTheme(){
  const actual=document.documentElement.getAttribute("data-theme");
  const nuevo=actual==="dark"?"light":"dark";
  localStorage.setItem("panel-general-theme",nuevo);
  aplicarTema(nuevo);
}
(function initTema(){
  const guardado=localStorage.getItem("panel-general-theme");
  const tema=guardado||(window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
  aplicarTema(tema);
})();

// ── NAVEGACIÓN ──
let vistaActual="dashboard";
function mostrarVista(vista){
  document.getElementById(`view-${vistaActual}`).style.display="none";
  document.getElementById(`nav-${vistaActual}`).classList.remove("active");
  vistaActual=vista;
  document.getElementById(`view-${vista}`).style.display="block";
  document.getElementById(`nav-${vista}`).classList.add("active");
  if(vista==="resenas") renderResenas();
  if(vista==="analitica") renderAnalitica();
}

function cambiarFiltroRestaurante(valor){
  restauranteFiltro=valor;
  document.getElementById('filtro-rest-pedidos').value=valor;
  document.getElementById('filtro-rest-resenas').value=valor;
  render();
  renderResenas();
}

function actualizarSelectRestaurantes(){
  const nombres=[...new Set(todos.map(p=>p.restaurante_nombre).filter(Boolean))].sort();
  const opciones='<option value="">Todos los restaurantes</option>'+nombres.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  ['filtro-rest-pedidos','filtro-rest-resenas'].forEach(id=>{
    const sel=document.getElementById(id);
    const actual=sel.value;
    sel.innerHTML=opciones;
    sel.value=actual;
  });
}

async function cargarPedidos(){
  try{
    const r=await fetch(`/api/pedidos?pw=${encodeURIComponent(pw)}`);
    if(r.status===403){window.location.href='/panel';return;}
    const d=await r.json();
    todos=d.pedidos;
    await cargarEstadosDom(todos);
    actualizarSelectRestaurantes();
    actualizarBadgeResenasBajas();
    render();stats();contadores();ventasHoy();
    if(vistaActual==="resenas") renderResenas();
    if(vistaActual==="analitica") renderAnalitica();
  }catch(e){console.error(e);}
}
function actualizarBadgeResenasBajas(){
  const bajas=todos.filter(p=>p.calificacion&&p.calificacion<=2).length;
  const badge=document.getElementById('badge-resenas-bajas');
  if(bajas>0){badge.textContent=bajas;badge.style.display='inline-block';}
  else{badge.style.display='none';}
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
  document.getElementById('s-hora').textContent=ahora;
  document.getElementById('s-total').textContent=`Total: ${todos.length}`;
  const act=todos.filter(p=>p.estado==='activo'||p.estado==='preparando').length;
  document.getElementById('s-activos').textContent=`Activos: ${act}`;
}
function contadores(){
  document.getElementById('c-todos').textContent=`(${todos.length})`;
  document.getElementById('c-prep').textContent=`(${todos.filter(p=>p.estado==='activo'||p.estado==='preparando').length})`;
  document.getElementById('c-env').textContent=`(${todos.filter(p=>p.estado==='enviado').length})`;
  document.getElementById('c-ent').textContent=`(${todos.filter(p=>p.estado==='entregado').length})`;
}
function filtrar(tab){
  let base=todos;
  if(restauranteFiltro) base=base.filter(p=>p.restaurante_nombre===restauranteFiltro);
  if(tab==='preparacion')return base.filter(p=>p.estado==='activo'||p.estado==='preparando');
  if(tab==='enviados')return base.filter(p=>p.estado==='enviado');
  if(tab==='entregados')return base.filter(p=>p.estado==='entregado');
  return base;
}
function cambiarTab(tab){
  tabActual=tab;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('on'));
  document.querySelector(`.tab[data-tab="${tab}"]`).classList.add('on');
  render();
}
const EST_MAP={activo:'Activo',preparando:'Pedido recibido',enviado:'Enviado',entregado:'Entregado',cancelado:'Cancelado'};
const BTNS=[
  {k:'activo',i:'●',c:'eb-activo'},{k:'preparando',i:'◐',c:'eb-preparando'},
  {k:'enviado',i:'▶',c:'eb-enviado'},{k:'entregado',i:'✓',c:'eb-entregado'},{k:'cancelado',i:'✕',c:'eb-cancelado'}
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
  if (btn) { btn.disabled = true; btn.textContent = "Buscando..."; }
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
    return `<div class="dom-asignado">Asignado a ${esc(est.nombre || 'domiciliario')}</div>`;
  }
  if (est.buscando) {
    return `<div class="dom-buscando">Buscando domiciliario...</div>`;
  }
  return `<button class="btn-buscar-dom" id="btn-dom-${p.id}" onclick="buscarDomiciliario('${p.id}')">Empezar a buscar domiciliario</button>`;
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
      <div class="rest-tag">${esc(p.restaurante_nombre||'—')}</div>
      <div class="hora">${esc(p.hora||'')}</div>
      <div class="tipo-tag">${p.tipo==='domicilio'?'Domicilio':'Recoger'}</div>
      <div class="cli">${esc(p.numero_cliente)}</div>
      ${p.cliente_nombre?`<div class="cli-nombre">${esc(p.cliente_nombre)}</div>`:''}
      <div class="dir">${esc(p.direccion)}</div>
      <div class="resumen">${esc(p.resumen||'')}</div>
      ${p.modificaciones&&p.modificaciones.length?`<div class="mods"><strong>Modificaciones:</strong><br>${p.modificaciones.map(esc).join('<br>')}</div>`:''}
      ${p.quejas&&p.quejas.length?`<div class="quejas-box"><strong>Quejas:</strong><br>${p.quejas.map(esc).join('<br>')}</div>`:''}
      <div class="est-lbl ${p.estado}">${EST_MAP[p.estado]||p.estado}</div>
      ${!esFinal ? htmlBotonDom(p) : ''}
      <div class="ebts">${BTNS.map(b=>{
        const idxBtn = orden.indexOf(b.k);
        const bloqueado = esFinal || (idxBtn < idxActual && b.k !== 'cancelado');
        return `<button class="eb ${b.c} ${p.estado===b.k?'on':''}"
          title="${b.k}"
          ${bloqueado ? 'disabled style="opacity:.25;cursor:not-allowed"' : ''}
          onclick="cambiarEstado('${p.id}','${b.k}')">${b.i}</button>`;
      }).join('')}</div>
    </div>`;
  }).join('');
}
async function cambiarEstado(id,estado){
  await fetch(`/api/pedidos/${id}/estado`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pw,estado})});
  cargarPedidos();
}

// ── RESEÑAS (a partir de calificacion/comentario_calificacion que ya trae cada pedido) ──
function renderResenas(){
  const cont=document.getElementById('resenas-lista');
  let conResena=todos.filter(p=>p.calificacion);
  if(restauranteFiltro) conResena=conResena.filter(p=>p.restaurante_nombre===restauranteFiltro);
  if(!conResena.length){cont.innerHTML='<div class="empty">Aún no hay reseñas de clientes</div>';return;}
  const porRest={};
  conResena.forEach(p=>{
    const nombre=p.restaurante_nombre||'—';
    (porRest[nombre]=porRest[nombre]||[]).push(p);
  });
  cont.innerHTML=Object.entries(porRest).map(([nombre,reseñas])=>{
    const prom=(reseñas.reduce((a,p)=>a+p.calificacion,0)/reseñas.length).toFixed(1);
    const items=reseñas.slice().sort((a,b)=>(b.fecha||'').localeCompare(a.fecha||'')).map(p=>`
      <div class="rev-item ${p.calificacion<=2?'rev-item-alert':''}">
        <div class="rev-stars">${'★'.repeat(p.calificacion)}${'☆'.repeat(5-p.calificacion)}</div>
        ${p.comentario_calificacion?`<div class="rev-comment">${esc(p.comentario_calificacion)}</div>`:'<div class="rev-comment" style="color:var(--text3);font-style:italic">Sin comentario, solo calificó con estrellas</div>'}
        <div class="rev-meta">${esc(p.cliente_nombre||'Cliente')} · pedido #${esc(p.id)}</div>
      </div>
    `).join('');
    return `
    <div class="rev-rest">
      <div class="rev-rest-header"><h3>${esc(nombre)}</h3><span class="rev-prom">★ ${prom} (${reseñas.length})</span></div>
      ${items}
    </div>`;
  }).join('');
}

function renderAnalitica(){
  const hoy=todos.filter(p=>esHoy(p.fecha)&&p.estado!=='cancelado');
  const porRest={};
  hoy.forEach(p=>{
    const nombre=p.restaurante_nombre||'—';
    porRest[nombre]=(porRest[nombre]||0)+(p.total||0);
  });
  const entries=Object.entries(porRest).sort((a,b)=>b[1]-a[1]);
  const totalHoy=entries.reduce((a,[,v])=>a+v,0);
  const top=entries[0];
  document.getElementById('analitica-resumen').innerHTML=`
    <div class="card-r"><span class="num">${entries.length}</span><span class="lbl">Restaurantes con ventas hoy</span></div>
    <div class="card-r total"><span class="num">$${Math.round(totalHoy).toLocaleString('es-CO')}</span><span class="lbl">Total vendido hoy</span></div>
    <div class="card-r"><span class="num" style="font-size:1.1rem">${top?esc(top[0]):'—'}</span><span class="lbl">Restaurante top de hoy</span></div>
  `;
  const chart=document.getElementById('chart-rest');
  if(!entries.length){chart.innerHTML='<p style="color:var(--text3);font-size:.85rem;margin:auto">Sin ventas registradas hoy todavía</p>';return;}
  const max=Math.max(...entries.map(([,v])=>v),1);
  chart.innerHTML=entries.map(([nombre,val])=>`
    <div class="bar-wrap">
      <div class="bar-val">$${Math.round(val).toLocaleString('es-CO')}</div>
      <div class="bar" style="height:${Math.max((val/max)*100,4)}%"></div>
      <div class="bar-lbl">${esc(nombre)}</div>
    </div>
  `).join('');
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
async def panel(request: Request, pw: str = ""):
    bloqueado, segundos = login_bloqueado(request, "panel_general")
    if bloqueado:
        return HTMLResponse(f"<h3 style='font-family:sans-serif;text-align:center;margin-top:80px'>Demasiados intentos fallidos. Espera {segundos} segundos e intenta de nuevo.</h3>")
    if pw == PANEL_PASSWORD:
        registrar_intento_exitoso(request, "panel_general")
        return HTMLResponse(PANEL_HTML.replace("{{PW}}", PANEL_PASSWORD))
    if pw:
        registrar_intento_fallido(request, "panel_general")
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

    # Si el pago es por Nequi/Bre-B y aún no se confirmó que llegó, no se puede
    # avanzar el pedido (evita que el restaurante empiece a cocinar sin cobrar).
    if nuevo != "cancelado" and pedido.get("metodo_pago") in ("nequi", "bre_b") and not pedido.get("pago_confirmado"):
        return {"ok": False, "msg": "Primero debes confirmar que el pago por transferencia llegó."}

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
        clientes_esperando_calificacion.setdefault(numero, []).append(pedido_id)
    if nuevo == "cancelado" and anterior != "cancelado":
        enviar_whatsapp(numero, f"❌ *Pedido #{pedido_id} cancelado.*\nSi tienes dudas contáctanos. ¡Hasta pronto! 🍔")
        restaurar_uso_codigo_si_aplica(pedido)
    return {"ok": True}

def _iniciar_busqueda_domiciliario(pedido):
    """Notifica a los domiciliarios disponibles para que acepten este pedido.
    Reutilizada por el panel general y por el panel propio de cada restaurante."""
    pedido_id = pedido["id"]
    if pedido.get("tipo") != "domicilio":
        return {"ok": False, "msg": "Este pedido no es de domicilio"}
    if pedido_ya_asignado(pedido_id):
        return {"ok": False, "msg": "Este pedido ya tiene domiciliario asignado"}

    doms = get_domiciliarios_disponibles()
    if not doms:
        return {"ok": False, "msg": "No hay domiciliarios disponibles en este momento"}
    try:
        supabase.table("pedidos").update({"busqueda_domiciliario": True}).eq("id", pedido_id).execute()
    except Exception:
        traceback.print_exc()
        return {"ok": False, "msg": "Falta la columna busqueda_domiciliario en la tabla pedidos (revisa el SQL pendiente en Supabase)"}
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
    buscando = False
    if not asignado:
        p = get_pedido_by_id(pedido_id)
        buscando = bool(
            p and p.get("tipo") == "domicilio"
            and p.get("estado") in ["activo", "preparando"]
            and p.get("busqueda_domiciliario")
        )
    return {"asignado": asignado, "nombre": nombre, "buscando": buscando}

@app.get("/api/pedidos/{pedido_id}/estado-domiciliario")
async def estado_domiciliario(pedido_id: str, pw: str = ""):
    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    return _obtener_estado_domiciliario_pedido(pedido_id)

# ── PANEL PROPIO POR RESTAURANTE ──────────────────────────────────────────────

@app.post("/panel-restaurante/login")
async def panel_restaurante_login(request: Request):
    bloqueado, segundos = login_bloqueado(request, "restaurante")
    if bloqueado:
        return {"ok": False, "msg": f"Demasiados intentos fallidos. Espera {segundos} segundos."}
    body = await request.json()
    rest_key = get_restaurante_key_por_password(body.get("password", ""))
    if not rest_key:
        registrar_intento_fallido(request, "restaurante")
        return {"ok": False, "msg": "Contraseña incorrecta"}
    registrar_intento_exitoso(request, "restaurante")
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

@app.post("/api/restaurante-panel/pedidos/{pedido_id}/confirmar-pago")
async def api_restaurante_panel_confirmar_pago(pedido_id: str, request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    pedido = get_pedido_by_id(pedido_id)
    if not pedido or pedido.get("restaurante_id") != rest_key:
        raise HTTPException(status_code=404)
    if pedido.get("estado") in ("entregado", "cancelado"):
        return {"ok": False, "msg": f"Este pedido ya fue {pedido['estado']} y no se puede modificar."}
    try:
        supabase.table("pedidos").update({"pago_confirmado": True}).eq("id", pedido_id).execute()
        enviar_whatsapp(pedido["numero_cliente"], f"✅ *¡Pago confirmado!* Ya estamos preparando tu pedido #{pedido_id} 🍔")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

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

@app.post("/api/restaurante-panel/menu/{item_id}/agotados")
async def api_restaurante_panel_agotados_item(item_id: int, request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        item_res = supabase.table("menu_items").select("restaurante_id").eq("id", item_id).execute()
        if not item_res.data or item_res.data[0]["restaurante_id"] != rest_key:
            raise HTTPException(status_code=404)
        productos = [p.strip() for p in body.get("productos_agotados", []) if p.strip()]
        supabase.table("menu_items").update({"productos_agotados": productos}).eq("id", item_id).execute()
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

@app.get("/api/restaurante-panel/codigos")
async def api_restaurante_panel_get_codigos(request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        res = supabase.table("codigos_descuento").select("*").eq("restaurante_id", rest_key).order("fecha_creacion", desc=True).execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/restaurante-panel/codigos")
async def api_restaurante_panel_crear_codigo(request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        codigo_texto = (body.get("codigo") or "").strip().upper()
        if not codigo_texto:
            return {"ok": False, "msg": "El código es obligatorio"}
        usos = int(body.get("usos_totales") or 1)
        fila = {
            "codigo": codigo_texto,
            "restaurante_id": rest_key,  # forzado al propio restaurante, no puede crear para otro
            "tipo": body.get("tipo") or "porcentaje",
            "valor": float(body.get("valor") or 0),
            "usos_totales": usos,
            "usos_restantes": usos,
            "activo": True,
            "monto_minimo": float(body.get("monto_minimo") or 0),
            "aplica_a": body.get("aplica_a") or "total",
            "fecha_expiracion": body.get("fecha_expiracion") or None,
        }
        supabase.table("codigos_descuento").insert(fila).execute()
        return {"ok": True}
    except Exception as e:
        error_txt = str(e).lower()
        if "duplicate" in error_txt or "unique" in error_txt:
            return {"ok": False, "msg": "Ya existe un código con ese texto"}
        return {"ok": False, "msg": str(e)}

@app.put("/api/restaurante-panel/codigos/{codigo_id}")
async def api_restaurante_panel_editar_codigo(codigo_id: int, request: Request):
    body = await request.json()
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        existente = supabase.table("codigos_descuento").select("*").eq("id", codigo_id).execute()
        if not existente.data or existente.data[0].get("restaurante_id") != rest_key:
            return {"ok": False, "msg": "Este código no te pertenece"}
        datos = {k: v for k, v in body.items() if k not in ["id", "codigo", "restaurante_id"]}
        if "valor" in datos: datos["valor"] = float(datos["valor"])
        if "usos_totales" in datos: datos["usos_totales"] = int(datos["usos_totales"])
        if "usos_restantes" in datos: datos["usos_restantes"] = int(datos["usos_restantes"])
        if "monto_minimo" in datos: datos["monto_minimo"] = float(datos["monto_minimo"])
        supabase.table("codigos_descuento").update(datos).eq("id", codigo_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.delete("/api/restaurante-panel/codigos/{codigo_id}")
async def api_restaurante_panel_eliminar_codigo(codigo_id: int, request: Request):
    rest_key = verificar_token_restaurante(request.cookies.get("rest_session", ""))
    if not rest_key:
        raise HTTPException(status_code=403)
    try:
        existente = supabase.table("codigos_descuento").select("*").eq("id", codigo_id).execute()
        if not existente.data or existente.data[0].get("restaurante_id") != rest_key:
            return {"ok": False, "msg": "Este código no te pertenece"}
        supabase.table("codigos_descuento").delete().eq("id", codigo_id).execute()
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
    if not dom:
        return {"pedido": None, "pedidos": [], "stats": {}, "disponible": False}
    # Las estadísticas (entregas y ganancias) se calculan siempre, esté o no
    # en turno — un domiciliario desconectado también quiere ver lo que lleva ganado.
    ganancias = calcular_ganancias_dom(dom["id"])
    try:
        hoy = date.today().isoformat()
        res = supabase.table("asignaciones").select("*")            .eq("domiciliario_id", dom["id"])            .gte("fecha", hoy).execute()
        pedidos_hoy = len(res.data or [])
    except Exception:
        pedidos_hoy = 0
    stats = {"pedidos_hoy": pedidos_hoy, **ganancias}
    if not dom.get("disponible"):
        return {"pedido": None, "pedidos": [], "stats": stats, "disponible": False}
    # Pedidos esperando domiciliario, directo desde la BD (sobrevive reinicios,
    # excluye cancelados/asignados, y muestra todos — no solo el primero)
    disponibles = get_pedidos_sin_asignar()
    for p in disponibles:
        cli = get_cliente(p.get("numero_cliente", ""))
        p["cliente_nombre"] = cli.get("nombre", "") if cli else ""
        r_pedido = get_restaurante(p.get("restaurante_id", ""))
        p["costo_domicilio"] = costo_domicilio(r_pedido)
        # Distancia real: solo si el cliente compartió ubicación (la dirección
        # trae un link de maps) y el restaurante tiene su propia ubicación
        # configurada. Si falta cualquiera de los dos, no se muestra (igual que hoy).
        p["distancia_km"] = None
        if r_pedido and r_pedido.get("ubicacion_gps"):
            lat_rest, lng_rest = extraer_lat_lng(r_pedido["ubicacion_gps"])
            lat_cli, lng_cli = extraer_lat_lng(p.get("direccion", ""))
            if lat_rest is not None and lat_cli is not None:
                p["distancia_km"] = round(distancia_km(lat_rest, lng_rest, lat_cli, lng_cli), 1)
    return {
        "pedido": disponibles[0] if disponibles else None,  # compatibilidad con app vieja
        "pedidos": disponibles,
        "stats": stats,
        "disponible": True,
    }

@app.post("/api/domiciliario/aceptar")
async def aceptar_pedido_dom(request: Request):
    body = await request.json()
    dom = verificar_sesion_dom(body.get("dom_id"), body.get("token"))
    if not dom:
        return {"ok": False, "msg": "Sesión inválida, vuelve a iniciar sesión"}
    nombre = dom["nombre"]
    pedido_id = body.get("pedido_id", "")
    pedido_check = get_pedido_by_id(pedido_id)
    if not pedido_check:
        return {"ok": False, "msg": "Este pedido ya no existe"}
    if pedido_check.get("tipo") != "domicilio":
        return {"ok": False, "msg": "Este pedido no es de domicilio"}
    if pedido_check.get("estado") not in ["activo", "preparando"]:
        return {"ok": False, "msg": f"Este pedido ya no está disponible (estado: {pedido_check.get('estado')})"}
    if not pedido_check.get("busqueda_domiciliario"):
        return {"ok": False, "msg": "El restaurante todavía no ha iniciado la búsqueda de domiciliario para este pedido"}
    if pedido_ya_asignado(pedido_id):
        return {"ok": False, "msg": "Este pedido ya fue tomado por otro domiciliario"}
    try:
        supabase.table("asignaciones").insert({
            "pedido_id": pedido_id,
            "domiciliario_id": dom["id"],
            "estado": "aceptado"
        }).execute()
        # Actualizar estado pedido
        actualizar_estado_pedido(pedido_id, "preparando")
        # Notificar al admin
        enviar_whatsapp(ADMIN_NUMBER,
            f"✅ *Pedido #{pedido_id} aceptado*\n"
            f"🛵 Domiciliario: *{nombre}*\n"
            f"👉 {os.getenv('PANEL_URL', '')}/panel")
        # Notificar al restaurante que ya hay domiciliario asignado
        r_pedido = get_restaurante(pedido_check.get("restaurante_id", ""))
        notificar_restaurante(r_pedido,
            f"🛵 *Domiciliario asignado al pedido #{pedido_id}*\n"
            f"*{nombre}* aceptó la entrega y va en camino a recogerlo.")
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
    if not pedido_asignado_a(pedido_id, dom["id"]):
        return {"ok": False, "msg": "Este pedido no está asignado a ti"}
    pedido = get_pedido_by_id(pedido_id)
    if not pedido:
        return {"ok": False, "msg": "Este pedido ya no existe"}
    if pedido.get("estado") == "entregado":
        # Ya se había marcado como entregado antes (ej. el domiciliario tocó el
        # botón varias veces porque tardó en responder) — no reenviamos los
        # avisos ni volvemos a sumar el contador de entregas.
        return {"ok": True}
    actualizar_estado_pedido(pedido_id, "entregado")
    enviar_whatsapp(pedido["numero_cliente"],
        f"🙌 *¡Pedido #{pedido_id} entregado!*\n"
        f"Esperamos que lo disfrutes 😊\n"
        f"¡Gracias por pedir en Ipiales Delivery!")
    enviar_whatsapp(pedido["numero_cliente"],
        "⭐ ¿Cómo calificarías el servicio? Responde del 1 al 5 (puedes agregar un comentario si quieres).")
    clientes_esperando_calificacion.setdefault(pedido["numero_cliente"], []).append(pedido_id)
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
                cli = get_cliente(p.get("numero_cliente", ""))
                p["cliente_nombre"] = cli.get("nombre", "") if cli else ""
                p["costo_domicilio"] = costo_domicilio(get_restaurante(p.get("restaurante_id", "")))
                pedidos.append(p)
        from datetime import date
        hoy = date.today().isoformat()
        res_hoy = supabase.table("asignaciones").select("*")            .eq("domiciliario_id", dom["id"]).gte("fecha", hoy).execute()
        return {"pedidos": pedidos, "stats": {"pedidos_hoy": len(res_hoy.data or [])}}
    except Exception:
        traceback.print_exc()
        return {"pedidos": [], "stats": {}}

@app.get("/api/domiciliario/historial")
async def historial_dom(dom_id: str = "", token: str = ""):
    """Últimas entregas completadas de este domiciliario, para que pueda
    ver cuánto ha entregado y ganado en días anteriores (no solo hoy)."""
    dom = verificar_sesion_dom(dom_id, token)
    if not dom:
        return {"pedidos": []}
    try:
        res = supabase.table("asignaciones").select("pedido_id")\
            .eq("domiciliario_id", dom["id"]).order("fecha", desc=True).limit(100).execute()
        ids = [a["pedido_id"] for a in (res.data or [])]
        entregados = []
        for pid in ids:
            p = get_pedido_by_id(pid)
            if p and p.get("estado") == "entregado":
                cli = get_cliente(p.get("numero_cliente", ""))
                p["cliente_nombre"] = cli.get("nombre", "") if cli else ""
                p["costo_domicilio"] = costo_domicilio(get_restaurante(p.get("restaurante_id", "")))
                entregados.append(p)
        entregados.sort(key=lambda p: p.get("fecha", ""), reverse=True)
        return {"pedidos": entregados[:30]}
    except Exception:
        traceback.print_exc()
        return {"pedidos": []}

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
    numero = None
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "ok"}

        mensaje = entry["messages"][0]

        # ── UBICACIÓN DE WHATSAPP (clip 📎 → Ubicación) ───────────────────────
        if mensaje.get("type") == "location":
            numero = mensaje["from"]
            cargar_sesion(numero)
            loc = mensaje.get("location", {}) or {}
            lat, lng = loc.get("latitude"), loc.get("longitude")
            nombre_lugar = (loc.get("name") or loc.get("address") or "").strip()
            if lat is None or lng is None:
                enviar_whatsapp(numero, "No pude leer tu ubicación 😅 Intenta enviarla de nuevo o escribe tu dirección.")
                return {"status": "ok"}

            # Radio de domicilio: solo se valida si el restaurante activo tiene su
            # propia ubicación configurada (si no, se comporta igual que antes, sin
            # validar nada). Si el cliente está fuera del radio, NO se guarda esta
            # ubicación como dirección — se le avisa y se le ofrece recoger o dar
            # otra dirección más cercana.
            rest_key_actual = cliente_restaurante.get(numero)
            r_actual = get_restaurante(rest_key_actual) if rest_key_actual else None
            if r_actual and r_actual.get("ubicacion_gps"):
                lat_rest, lng_rest = extraer_lat_lng(r_actual["ubicacion_gps"])
                if lat_rest is not None:
                    dist = distancia_km(lat, lng, lat_rest, lng_rest)
                    radio = float(r_actual.get("radio_domicilio_km") or 8)
                    if dist > radio:
                        dist_txt = f"{dist:,.1f}".replace(",", ".")
                        enviar_whatsapp(numero,
                            f"📍 Tu ubicación está a {dist_txt} km de *{r_actual['nombre']}* — fuera de nuestro "
                            f"radio de entrega ({radio:.0f} km).\n"
                            f"Escribe otra dirección más cercana o *recoger* para recoger en el local."
                        )
                        return {"status": "ok"}

            link_maps = f"https://maps.google.com/?q={lat},{lng}"
            direccion_txt = f"{nombre_lugar} — {link_maps}" if nombre_lugar else link_maps
            ubicacion_reciente[numero] = direccion_txt

            # Si está en pleno registro y le pedimos su dirección, la usamos ahí
            if numero in clientes_registrando and clientes_registrando[numero].get("paso") == "direccion":
                nombre_reg = clientes_registrando[numero]["nombre"]
                crear_cliente(numero, nombre_reg, direccion_txt)
                clientes_registrando.pop(numero)
                enviar_whatsapp(numero,
                    f"✅ *¡Listo {nombre_reg}, ya estás registrado!*\n"
                    f"📍 Guardamos tu ubicación para las entregas.\n\n"
                    f"Ahora elige un restaurante 👇")
                enviar_whatsapp(numero, lista_restaurantes())
                return {"status": "ok"}

            # Si está en medio de un pedido, la inyectamos a la conversación
            # para que el bot la use como dirección de entrega
            if numero in historial:
                historial[numero].append({"role": "user", "content": f"Mi ubicación exacta para la entrega es: {direccion_txt}"})
                historial[numero].append({"role": "assistant", "content": "📍 ¡Ubicación recibida! La usaré como dirección de entrega de tu pedido."})
            enviar_whatsapp(numero, "📍 *¡Ubicación recibida!* La usaremos como tu dirección de entrega 😊")
            return {"status": "ok"}

        if mensaje.get("type") != "text":
            numero = mensaje["from"]
            cargar_sesion(numero)
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
        cargar_sesion(numero)
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

        # ── RESTAURANTE ESCRIBIENDO AL NÚMERO DEL BOT (no a su panel) ──
        # Este número (el que recibe pedidos de clientes) no distingue
        # restaurantes — sin este aviso, el dueño terminaría metido en el
        # flujo de pedir comida por accidente en vez de ir a su panel.
        r_notif = get_restaurante_por_numero_notificacion(numero)
        if r_notif:
            enviar_whatsapp(numero,
                f"👋 Hola, este número es para que tus *clientes* hagan pedidos — no es donde gestionas "
                f"*{r_notif['nombre']}*.\n\n"
                f"Para ver y manejar tus pedidos, entra a tu panel:\n"
                f"{os.getenv('PANEL_URL', '')}/panel-restaurante"
            )
            return {"status": "ok"}

        # ── CALIFICACIÓN DE SERVICIO ───────────────────────────────────────────
        if clientes_esperando_calificacion.get(numero):
            m = re.match(r"^\s*([1-5])(?:\s+(.*))?$", texto.strip(), re.DOTALL)
            if m:
                cola = clientes_esperando_calificacion[numero]
                pedido_id_cal = cola.pop(0)  # el primero que se entregó, primero se califica
                if not cola:
                    clientes_esperando_calificacion.pop(numero, None)
                calificacion = int(m.group(1))
                comentario = (m.group(2) or "").strip() or None
                guardar_calificacion(pedido_id_cal, calificacion, comentario)
                extra = f"\n\nTe queda {len(cola)} pedido(s) más por calificar — responde otro número del 1 al 5 😊" if cola else ""
                enviar_whatsapp(numero, "¡Gracias por tu calificación! 🙏" + (" Tomamos nota de tu comentario." if comentario else "") + extra)
                return {"status": "ok"}
            # Si no es un número del 1 al 5, no bloqueamos nada: dejamos la espera
            # activa y el mensaje sigue su flujo normal (ej. el cliente quiere pedir de nuevo).

        # ── RESPONDIENDO "CUÁL PEDIDO" (cancelar/modificar/queja con varios activos) ──
        if numero in clientes_esperando_cual_pedido:
            pendiente = clientes_esperando_cual_pedido.pop(numero)
            m = re.match(r"^\s*(\d+)", texto.strip())
            idx = int(m.group(1)) - 1 if m else -1
            opciones = pendiente["pedidos"]
            if 0 <= idx < len(opciones):
                pedido_elegido = opciones[idx]
                accion = pendiente["accion"]
                if accion == "cancelar":
                    ejecutar_cancelar_pedido(pedido_elegido, numero)
                elif accion == "modificar":
                    ejecutar_modificar_pedido(pedido_elegido, numero, pendiente["texto"])
                elif accion == "queja":
                    ejecutar_queja_pedido(pedido_elegido, numero, pendiente["texto"])
            else:
                enviar_whatsapp(numero, "No entendí cuál pedido — escribe de nuevo el comando (cancelar pedido / modificar pedido / queja) cuando quieras intentarlo otra vez.")
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
            codigo_aplicado.pop(numero, None)  # un código de otro restaurante no debe seguir activo aquí
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
                f"Aquí tienes el menú 👇")
            enviar_menu_segun_modo(numero, rest_key)
            enviar_whatsapp(numero,
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
            codigo_aplicado.pop(numero, None)
            clientes_eligiendo[numero] = True
            enviar_whatsapp(numero, "😔 Ese restaurante ya no está disponible.\n\n" + lista_restaurantes())
            return {"status": "ok"}

        if not esta_abierto(rest_key) and numero != ADMIN_NUMBER:
            enviar_whatsapp(numero, f"😔 *{r_actual['nombre']}* cerró. Horario: {r_actual['hora_inicio']}:00–{r_actual['hora_fin']}:00. ¡Hasta mañana! 🙏")
            cliente_restaurante.pop(numero, None)
            codigo_aplicado.pop(numero, None)
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
                pedido, resuelto = resolver_pedido_o_preguntar(numero, "cancelar", texto)
                if not resuelto:
                    return {"status": "ok"}  # se le preguntó cuál pedido, esperando respuesta
                if pedido:
                    ejecutar_cancelar_pedido(pedido, numero)
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo para cancelar.")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["modificar pedido", "cambiar pedido", "agregar algo"]):
                pedido, resuelto = resolver_pedido_o_preguntar(numero, "modificar", texto)
                if not resuelto:
                    return {"status": "ok"}
                if pedido:
                    ejecutar_modificar_pedido(pedido, numero, texto)
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo.")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["queja", "reclamación", "problema con mi pedido", "está mal"]):
                pedido, resuelto = resolver_pedido_o_preguntar(numero, "queja", texto)
                if not resuelto:
                    return {"status": "ok"}
                if pedido:
                    ejecutar_queja_pedido(pedido, numero, texto)
                else:
                    enviar_whatsapp(numero, "Cuéntanos qué pasó 😊")
                return {"status": "ok"}

            if any(p in texto_lower for p in ["el menú", "el menu", "menú", "menu", "carta", "pdf"]):
                enviar_menu_segun_modo(numero, rest_key)
                enviar_whatsapp(numero, "¿Qué te gustaría pedir? 😊")
                return {"status": "ok"}

            m_kw_codigo = re.search(r"c[oó]digo", texto, re.IGNORECASE)
            if m_kw_codigo:
                # Buscamos el primer token "tipo código" DESPUÉS de la palabra "código",
                # ignorando palabras de relleno comunes (así reconoce frases naturales
                # como "el código es FRISBY20", no solo "código: FRISBY20" pegado).
                palabras_relleno = {
                    "es", "de", "un", "una", "tengo", "mi", "el", "la", "para", "descuento",
                    "promocional", "promo", "tiene", "dame", "seria", "sería", "uso", "usar", "codigo", "código",
                }
                codigo_texto = None
                for tok in re.findall(r"[a-zA-Z0-9_-]{3,20}", texto[m_kw_codigo.end():]):
                    if tok.lower() not in palabras_relleno:
                        codigo_texto = tok.upper()
                        break
                if codigo_texto:
                    fila, error = validar_codigo_descuento(codigo_texto, rest_key, numero)
                    if fila:
                        codigo_aplicado[numero] = fila
                        enviar_whatsapp(numero, f"✅ Código *{codigo_texto}* aplicado: {descripcion_descuento(fila)}. Se descontará de tu total al confirmar el pedido.")
                    else:
                        enviar_whatsapp(numero, f"❌ {error}")
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

        # Defensa extra: un código reservado para OTRO restaurante nunca debe
        # colarse aquí, sin importar por dónde haya quedado guardado.
        _descuento_actual = codigo_aplicado.get(numero)
        if _descuento_actual and _descuento_actual.get("restaurante_id") and _descuento_actual["restaurante_id"] != rest_key:
            codigo_aplicado.pop(numero, None)
            _descuento_actual = None

        ai = anthropic.Anthropic(api_key=CLAUDE_KEY)
        resp = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=build_system_prompt(rest_key, cliente, _descuento_actual),
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
            # La búsqueda de domiciliario ya NO es automática: el restaurante la
            # dispara a propósito con el botón "Buscar domiciliario" en su panel
            # (para no avisarles de un pedido que el restaurante ni siquiera ha
            # revisado o aceptado todavía).

    except Exception:
        traceback.print_exc()
    finally:
        guardar_sesion(numero)

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
            "categoria": body.get("categoria", ""),
            "descripcion": body.get("descripcion", ""),
            "logo_url": body.get("logo_url", ""),
            "nequi_numero": body.get("nequi_numero", ""),
            "llave_bre_b": body.get("llave_bre_b", ""),
            "costo_domicilio": int(body.get("costo_domicilio") or 3000),
            "ubicacion_gps": body.get("ubicacion_gps", ""),
            "radio_domicilio_km": float(body.get("radio_domicilio_km") or 8),
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
        if "radio_domicilio_km" in datos: datos["radio_domicilio_km"] = float(datos["radio_domicilio_km"] or 8)
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

@app.post("/api/admin/restaurantes/{rest_id}/foto")
async def admin_subir_foto_restaurante(rest_id: str, request: Request, foto: UploadFile = File(...)):
    check_admin(request)
    try:
        contenido = await foto.read()
        ext = os.path.splitext(foto.filename or "")[1].lower() or ".jpg"
        ruta_storage = f"{rest_id}{ext}"
        supabase.storage.from_(STORAGE_BUCKET_FOTOS).upload(
            ruta_storage,
            contenido,
            {"content-type": foto.content_type or "image/jpeg", "upsert": "true"},
        )
        url_publica = supabase.storage.from_(STORAGE_BUCKET_FOTOS).get_public_url(ruta_storage)
        supabase.table("restaurantes").update({"logo_url": url_publica}).eq("id", rest_id).execute()
        cargar_restaurantes()
        return {"ok": True, "logo_url": url_publica}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "msg": str(e)}

# ── API PÚBLICA: RESTAURANTES PARA LA LANDING ────────────────────────────────

@app.get("/api/restaurantes-publicos")
async def restaurantes_publicos():
    try:
        res = supabase.table("restaurantes").select(
            "id,nombre,categoria,descripcion,logo_url"
        ).eq("activo", True).order("nombre").execute()
        data = []
        for r in (res.data or []):
            nombre = r.get("nombre", "")
            mensaje = quote(f"Hola, quiero pedir de {nombre}")
            data.append({
                "id": r["id"],
                "nombre": nombre,
                "categoria": r.get("categoria") or "",
                "descripcion": r.get("descripcion") or "",
                "logo_url": r.get("logo_url") or "",
                "whatsapp": f"https://wa.me/{WHATSAPP_MAIN_NUMBER}?text={mensaje}",
            })
        return {"ok": True, "data": data}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "msg": str(e)}

@app.get("/api/restaurantes-publicos/{rest_id}/menu")
async def menu_publico(rest_id: str):
    """Menú de un restaurante para mostrar en la landing pública (sin login).
    Devuelve solo categorías activas y no ocultas hoy, con los productos
    agotados de hoy marcados aparte (no mezclados en el texto)."""
    r = get_restaurante(rest_id)
    if not r or not r.get("activo", True):
        return {"ok": False, "msg": "Restaurante no encontrado"}
    extra = _estado_extra.get(rest_id, {})
    desact = extra.get("categorias_desactivadas", set())
    items = get_menu(rest_id)
    data = [
        {
            "categoria": i["categoria"],
            "descripcion": i["descripcion"],
            "agotados_hoy": i.get("productos_agotados") or [],
        }
        for i in items if i["activo"] and i["categoria"] not in desact
    ]
    return {
        "ok": True,
        "nombre": r["nombre"],
        "costo_domicilio": costo_domicilio(r),
        "whatsapp_numero": WHATSAPP_MAIN_NUMBER,
        "menu_modo": r.get("menu_modo") or "texto",
        "menu_url": r.get("menu_url") or "",
        "data": data,
    }

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
            "telefono": normalizar_telefono(body["telefono"]),
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
        if "telefono" in datos:
            datos["telefono"] = normalizar_telefono(datos["telefono"])
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

# ── APIs ADMIN: CÓDIGOS DE DESCUENTO ──────────────────────────────────────────

@app.get("/api/admin/codigos")
async def admin_get_codigos(request: Request):
    check_admin(request)
    try:
        res = supabase.table("codigos_descuento").select("*").order("fecha_creacion", desc=True).execute()
        return {"ok": True, "data": res.data or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/admin/codigos")
async def admin_crear_codigo(request: Request):
    body = await request.json()
    check_admin(request)
    try:
        codigo_texto = (body.get("codigo") or "").strip().upper()
        if not codigo_texto:
            return {"ok": False, "msg": "El código es obligatorio"}
        usos = int(body.get("usos_totales") or 1)
        fila = {
            "codigo": codigo_texto,
            "restaurante_id": body.get("restaurante_id") or None,
            "tipo": body.get("tipo") or "porcentaje",
            "valor": float(body.get("valor") or 0),
            "usos_totales": usos,
            "usos_restantes": usos,
            "activo": True,
            "monto_minimo": float(body.get("monto_minimo") or 0),
            "aplica_a": body.get("aplica_a") or "total",
            "fecha_expiracion": body.get("fecha_expiracion") or None,
        }
        supabase.table("codigos_descuento").insert(fila).execute()
        return {"ok": True}
    except Exception as e:
        error_txt = str(e).lower()
        if "duplicate" in error_txt or "unique" in error_txt:
            return {"ok": False, "msg": "Ya existe un código con ese texto"}
        return {"ok": False, "msg": str(e)}

@app.put("/api/admin/codigos/{codigo_id}")
async def admin_editar_codigo(codigo_id: int, request: Request):
    body = await request.json()
    check_admin(request)
    try:
        datos = {k: v for k, v in body.items() if k not in ["id", "codigo"]}
        if "valor" in datos: datos["valor"] = float(datos["valor"])
        if "usos_totales" in datos: datos["usos_totales"] = int(datos["usos_totales"])
        if "usos_restantes" in datos: datos["usos_restantes"] = int(datos["usos_restantes"])
        if "monto_minimo" in datos: datos["monto_minimo"] = float(datos["monto_minimo"])
        supabase.table("codigos_descuento").update(datos).eq("id", codigo_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.delete("/api/admin/codigos/{codigo_id}")
async def admin_eliminar_codigo(codigo_id: int, request: Request):
    check_admin(request)
    try:
        supabase.table("codigos_descuento").delete().eq("id", codigo_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def _nombre_producto(linea):
    """Deja solo el nombre del producto en una línea de pedido, quitando precio
    y marcadores de cantidad (1x / x1), para poder agrupar por nombre."""
    s = re.sub(r"\$\s?[\d.]+.*$", "", linea)
    s = re.sub(r"^\s*\d+\s*[xX]\s*", "", s)
    s = re.sub(r"\s*[xX]\s*\d+\s*$", "", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return normalizar_texto(s).strip()

def top_productos_pedidos(pedidos, dias=30, top_n=5):
    """Cuenta los productos más pedidos en los últimos N días, a partir del
    campo "productos" que ya se guarda en cada pedido (best-effort: solo
    cuenta para dar una idea, no valida contra el menú como verificar_subtotal)."""
    limite = (datetime.now(ZONA_HORARIA) - timedelta(days=dias)).isoformat()
    conteo = {}
    for p in pedidos:
        if p.get("estado") == "cancelado":
            continue
        if p.get("fecha", "") < limite:
            continue
        texto = p.get("productos") or ""
        if not texto:
            continue
        for linea in texto.split("\n"):
            linea = linea.strip()
            if not linea:
                continue
            nombre = _nombre_producto(linea)
            if not nombre:
                continue
            conteo[nombre] = conteo.get(nombre, 0) + _extraer_cantidad(linea)
    return sorted(conteo.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

def ventas_por_dia_pedidos(pedidos, dias=7):
    """Total vendido (sin cancelados) por cada uno de los últimos N días."""
    hoy = datetime.now(ZONA_HORARIA).date()
    resultado = []
    for i in range(dias - 1, -1, -1):
        d = hoy - timedelta(days=i)
        clave = d.isoformat()
        total = sum(
            p.get("total", 0) for p in pedidos
            if p.get("estado") != "cancelado" and p.get("fecha", "").startswith(clave)
        )
        resultado.append({"dia": clave, "total": total})
    return resultado

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
                "ventas_por_dia": ventas_por_dia_pedidos(todos),
                "top_productos": top_productos_pedidos(todos),
            }
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ── PANEL ADMIN HTML ──────────────────────────────────────────────────────────

@app.post("/admin/login")
async def admin_login(request: Request):
    bloqueado, segundos = login_bloqueado(request, "admin")
    if bloqueado:
        return {"ok": False, "msg": f"Demasiados intentos fallidos. Espera {segundos} segundos."}
    body = await request.json()
    if body.get("password", "") != ADMIN_PASSWORD:
        registrar_intento_fallido(request, "admin")
        return {"ok": False, "msg": "Contraseña incorrecta"}
    registrar_intento_exitoso(request, "admin")
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
<title>Admin — Caza Delivery</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f8f9fa;--surface:#ffffff;--border:#e0e4e8;--text:#1f2937;--text2:#6b7280;
  --accent:#2563eb;--accent2:#1d4ed8;--accent-ring:rgba(37,99,235,.15);--red:#ef4444;
}
:root[data-theme="dark"]{
  --bg:#0f1419;--surface:#171d24;--border:#2a2f37;--text:#e8e8e8;--text2:#94a3b8;--red:#f28b8b;
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme="light"]):not([data-theme="dark"]){
    --bg:#0f1419;--surface:#171d24;--border:#2a2f37;--text:#e8e8e8;--text2:#94a3b8;--red:#f28b8b;
  }
}
body{background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text);transition:background .2s ease,color .2s ease}
.box{background:var(--surface);border:1px solid var(--border);box-shadow:0 10px 15px -3px rgba(0,0,0,.1);padding:32px 28px;border-radius:16px;text-align:center;width:90%;max-width:340px}
.logo{width:48px;height:48px;margin:0 auto 14px;background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;color:#fff}
h1{font-size:1.2rem;margin-bottom:4px;font-weight:700}
h1 span{background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
p{color:var(--text2);margin-bottom:20px;font-size:.87rem}
input{width:100%;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:1rem;outline:none;margin-bottom:12px;font-family:inherit}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}
button{width:100%;padding:12px;background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);border:none;border-radius:10px;color:#fff;font-weight:700;font-size:1rem;cursor:pointer;transition:filter .15s ease}
button:hover{filter:brightness(1.08)}
.err{color:var(--red);font-size:.82rem;margin:-6px 0 12px;display:none}
.theme-toggle{margin-top:16px;background:none;border:none;color:var(--text2);font-size:.78rem;cursor:pointer;width:auto;padding:6px}
.theme-toggle:hover{color:var(--text)}
</style></head><body>
<div class="box">
  <div class="logo">A</div>
  <h1>Panel <span>Admin</span></h1>
  <p>Caza Delivery</p>
  <form id="form-login">
    <input type="password" id="pw" placeholder="Contraseña admin" autofocus>
    <div class="err" id="err">Contraseña incorrecta</div>
    <button type="submit">Entrar</button>
  </form>
  <button class="theme-toggle" onclick="toggleTemaLogin()" id="theme-toggle-btn"><span id="theme-toggle-icon">☾</span> <span id="theme-toggle-label">Modo oscuro</span></button>
</div>
<script>
function aplicarTemaLogin(tema) {
  document.documentElement.setAttribute("data-theme", tema);
  document.getElementById("theme-toggle-icon").textContent = tema === "dark" ? "☀" : "☾";
  document.getElementById("theme-toggle-label").textContent = tema === "dark" ? "Modo claro" : "Modo oscuro";
}
function toggleTemaLogin() {
  const actual = document.documentElement.getAttribute("data-theme");
  const nuevo = actual === "dark" ? "light" : "dark";
  localStorage.setItem("admin-theme", nuevo);
  aplicarTemaLogin(nuevo);
}
(function initTemaLogin() {
  const guardado = localStorage.getItem("admin-theme");
  const tema = guardado || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  aplicarTemaLogin(tema);
})();

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
