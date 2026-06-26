from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import anthropic, requests, os, traceback, uuid, re
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz

load_dotenv()

app = FastAPI()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

CLAUDE_KEY = os.getenv("CLAUDE_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "ipiales2024")

ADMIN_NUMBER = "573167731698"
ZONA_HORARIA = pytz.timezone("America/Bogota")

historial = {}
mensajes_procesados = set()
clientes_esperando_decision = {}
# numero -> restaurante elegido
cliente_restaurante = {}
# numero -> True si está en proceso de elegir restaurante
clientes_eligiendo = {}

INTERVALO_CORTO_MINUTOS = 15

# ── RESTAURANTES ──────────────────────────────────────────────────────────────
RESTAURANTES = {
    "las_bravas": {
        "nombre": "Las Bravas",
        "direccion": "Cra 4 #15-32, Ipiales",
        "hora_inicio": 13,
        "hora_fin": 23,
        "abierto_forzado": False,
        "fecha_forzado": None,
        "domicilio_activo": True,
        "tiempo_espera": None,
        "categorias_desactivadas": set(),
        "notas": [],
        "menu": {
            "salchipapas": (
                "Salchipapas: Sencilla $10.000 / Especial (salchicha+tocino) $14.000 / "
                "Mixta (pollo+res) $17.000 / Trifásica (pollo+res+tocino) $22.000 / XXL $28.000"
            ),
            "hamburguesas": (
                "Hamburguesas: Sencilla $15.000 / Doble Carne $22.000 / "
                "Especial (piña+tocino) $20.000 / Ranchera (jalapeño+cebolla crispy) $25.000 / BBQ $23.000"
            ),
            "bebidas": (
                "Bebidas: Gaseosa personal $3.500 / Gaseosa 400ml $5.000 / Agua $3.000 / "
                "Jugo natural $5.000 / Limonada $4.500 / Malteada $8.000 / Té frío $4.000"
            ),
        },
    },
    "escarabajo": {
        "nombre": "Escarabajo Burgers",
        "direccion": "Calle 12 #7-18, Ipiales",
        "hora_inicio": 12,
        "hora_fin": 22,
        "abierto_forzado": False,
        "fecha_forzado": None,
        "domicilio_activo": True,
        "tiempo_espera": None,
        "categorias_desactivadas": set(),
        "notas": [],
        "menu": {
            "hamburguesas": (
                "Hamburguesas: Clásica $16.000 / Doble Smash $26.000 / "
                "Escarabajo Especial (doble carne+queso americano+salsa secreta) $28.000 / "
                "Mushroom Swiss $24.000 / Pollo Crispy $20.000"
            ),
            "papas": (
                "Papas: Papas Fritas Pequeñas $6.000 / Papas Fritas Grandes $10.000 / "
                "Papas con Queso $12.000 / Papas con Cheddar y Tocino $15.000 / Aros de Cebolla $9.000"
            ),
            "combos": (
                "Combos: Combo Clásico (Clásica+Papas+Gaseosa) $24.000 / "
                "Combo Doble (Doble Smash+Papas Grandes+Gaseosa) $36.000 / "
                "Combo Especial (Escarabajo Especial+Papas con Queso+Gaseosa) $38.000 / "
                "Combo Pollo (Pollo Crispy+Papas+Gaseosa) $28.000"
            ),
            "gaseosas": (
                "Gaseosas: Personal 250ml $3.500 / Mediana 400ml $5.000 / 1 Litro $8.000 / Agua $3.000"
            ),
        },
    },
    "monaco": {
        "nombre": "Mónaco Pizzas",
        "direccion": "Av Principal #22-45, Ipiales",
        "hora_inicio": 17,
        "hora_fin": 23,
        "abierto_forzado": False,
        "fecha_forzado": None,
        "domicilio_activo": True,
        "tiempo_espera": None,
        "categorias_desactivadas": set(),
        "notas": [],
        "menu": {
            "pizzas_personales": (
                "Pizzas Personales (1 porción): Margarita $8.000 / Pepperoni $10.000 / "
                "Hawaiana $10.000 / Cuatro Quesos $11.000 / BBQ Pollo $12.000 / "
                "Vegetariana $10.000 / Especial Mónaco (pepperoni+champiñón+extra queso) $13.000"
            ),
            "pizzas_medianas": (
                "Pizzas Medianas (4 porciones): Margarita $22.000 / Pepperoni $26.000 / "
                "Hawaiana $26.000 / Cuatro Quesos $28.000 / BBQ Pollo $30.000 / "
                "Vegetariana $26.000 / Especial Mónaco $32.000"
            ),
            "pizzas_grandes": (
                "Pizzas Grandes (8 porciones): Margarita $38.000 / Pepperoni $44.000 / "
                "Hawaiana $44.000 / Cuatro Quesos $48.000 / BBQ Pollo $52.000 / "
                "Vegetariana $44.000 / Especial Mónaco $56.000"
            ),
            "bebidas": (
                "Bebidas: Gaseosa personal $3.500 / Gaseosa 400ml $5.000 / "
                "Gaseosa 1L $8.000 / Agua $3.000 / Jugo natural $5.000 / "
                "Limonada $4.500 / Cerveza $7.000"
            ),
        },
    },
}

# ── HELPERS RESTAURANTES ──────────────────────────────────────────────────────

def esta_abierto(rest_key):
    r = RESTAURANTES[rest_key]
    ahora = datetime.now(ZONA_HORARIA)
    if r["abierto_forzado"] and r["fecha_forzado"] == ahora.date():
        return True
    if r["abierto_forzado"] and r["fecha_forzado"] != ahora.date():
        r["abierto_forzado"] = False
        r["fecha_forzado"] = None
    return r["hora_inicio"] <= ahora.hour < r["hora_fin"]


def lista_restaurantes():
    lineas = ["🍽️ *Bienvenido a Ipiales Delivery*\n\nElige un restaurante:\n"]
    for i, (key, r) in enumerate(RESTAURANTES.items(), 1):
        estado = "✅ Abierto" if esta_abierto(key) else "❌ Cerrado"
        lineas.append(f"{i}. *{r['nombre']}* — {estado}\n   📍 {r['direccion']}")
    lineas.append("\nResponde con el *número* (1, 2 o 3) para elegir.")
    return "\n".join(lineas)


# ── PEDIDOS ───────────────────────────────────────────────────────────────────
pedidos = []


def pedido_es_reciente(pedido):
    try:
        hora_pedido = datetime.fromisoformat(pedido["hora_iso"])
        return (datetime.now(ZONA_HORARIA) - hora_pedido) <= timedelta(minutes=INTERVALO_CORTO_MINUTOS)
    except Exception:
        return False


def registrar_pedido(numero_cliente, resumen, confirmacion_bot, rest_key):
    r = RESTAURANTES[rest_key]
    es_domicilio = any(p in confirmacion_bot.lower() for p in ["camino", "domicilio"])
    tipo = "domicilio" if es_domicilio else "recoger"
    direccion = ""
    if es_domicilio:
        texto = confirmacion_bot.lower()
        for marca in ["domicilio a", "a la dirección"]:
            if marca in texto:
                inicio = confirmacion_bot.lower().index(marca) + len(marca)
                direccion = confirmacion_bot[inicio:].split(".")[0].strip()
                break
    ahora = datetime.now(ZONA_HORARIA)
    existente = buscar_pedido_cliente(numero_cliente)
    if existente:
        existente.update({
            "resumen": resumen, "confirmacion": confirmacion_bot,
            "direccion": direccion if direccion else existente["direccion"],
            "tipo": tipo, "estado": "activo",
            "hora": ahora.strftime("%I:%M %p"), "hora_iso": ahora.isoformat(),
        })
        return existente, False
    pedido = {
        "id": str(uuid.uuid4())[:8].upper(),
        "numero": numero_cliente,
        "restaurante": r["nombre"],
        "hora": ahora.strftime("%I:%M %p"),
        "hora_iso": ahora.isoformat(),
        "resumen": resumen,
        "confirmacion": confirmacion_bot,
        "direccion": direccion if direccion else ("En local" if tipo == "recoger" else "Ver resumen"),
        "tipo": tipo,
        "estado": "activo",
        "modificaciones": [],
        "quejas": [],
    }
    pedidos.append(pedido)
    if len(pedidos) > 200:
        pedidos.pop(0)
    return pedido, True


def buscar_pedido_cliente(numero_cliente):
    for p in reversed(pedidos):
        if p["numero"] == numero_cliente and p["estado"] in ["activo", "preparando"]:
            return p
    return None


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

def build_system_prompt(rest_key):
    r = RESTAURANTES[rest_key]
    menu_activo = [v for k, v in r["menu"].items() if k not in r["categorias_desactivadas"]]
    notas = ("\nNOTAS ESPECIALES DE HOY:\n- " + "\n- ".join(r["notas"])) if r["notas"] else ""
    espera = f"\nTIEMPO DE ESPERA ACTUAL: {r['tiempo_espera']} minutos. Infórmalo al confirmar." if r["tiempo_espera"] else ""
    domicilio_txt = (
        "Sí. Costo: $3.000. Sin mínimo. Horario igual al de atención."
        if r["domicilio_activo"] else
        "No disponible. Solo atención en local."
    )
    return f"""Eres el asistente virtual de *{r['nombre']}*, restaurante ubicado en {r['direccion']}, Ipiales.
HORARIO: {r['hora_inicio']}:00 – {r['hora_fin']}:00
DOMICILIO: {domicilio_txt}
MÉTODOS DE PAGO: Nequi, Daviplata, transferencia, efectivo.
MENÚ:
{chr(10).join(menu_activo)}
{notas}{espera}

Si el cliente pide ver "el menú", "la carta" o "el pdf", NO se lo describas tú: el sistema ya le envía automáticamente el PDF del menú antes de que tú respondas.

INSTRUCCIONES CRÍTICAS PARA MANEJO DEL PEDIDO:
- Habla amigable y natural como empleado real de {r['nombre']}.
- Acumula TODOS los productos que el cliente pide sin mostrar resumen parcial.
- NUNCA muestres resumen ni total hasta que el cliente diga "es todo", "eso sería", "listo", "ya es todo", "nada más" o similar.
- Solo entonces muestra el resumen completo con todos los productos y el total.

INSTRUCCIONES SOBRE LA DIRECCIÓN (MUY IMPORTANTE):
- Si el cliente YA mencionó en cualquier parte de la conversación un lugar de entrega, eso significa que el pedido es DOMICILIO y esa es la dirección. NO preguntes "¿es domicilio o para recoger?".
- En ese caso, confirma esa dirección antes de cerrar el pedido.
- Una vez el cliente confirme, cierra el pedido con: "Perfecto, domicilio a [dirección]. Tu pedido ya está en camino 🛵"
- Solo si el cliente NO ha mencionado ningún lugar de entrega, pregunta: "¿Es para domicilio o para recoger en el local?"
- Si es para recoger, confirma con: "Perfecto, tu pedido estará listo para recoger en {r['direccion']} 🍔"
- No repitas el resumen ni el total después de confirmar.
- No inventes productos ni precios. Si no sabes algo, sugiere llamar.
- Si quiere hablar con persona real, dile que lo comunicas con el equipo.
- Responde siempre en español. Sé conciso."""


# ── NOTIFICACIONES ────────────────────────────────────────────────────────────

def notificar_pedido_admin(numero_cliente, pedido):
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    msg = (
        f"🛎️ *Pedido nuevo #{pedido['id']}*\n"
        f"🍽️ Restaurante: {pedido['restaurante']}\n"
        f"📱 Cliente: +{numero_cliente}\n"
        f"🕐 Hora: {pedido['hora']}\n"
        f"{icono} Tipo: {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger en local'}\n"
        f"📍 Dirección: {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}\n"
        f"────────────────\n"
        f"👉 Ver panel: {os.getenv('PANEL_URL', '')}/panel"
    )
    enviar_whatsapp(ADMIN_NUMBER, msg)


def notificar_pedido_actualizado_admin(numero_cliente, pedido):
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    msg = (
        f"🔄 *Pedido #{pedido['id']} actualizado*\n"
        f"🍽️ Restaurante: {pedido['restaurante']}\n"
        f"📱 Cliente: +{numero_cliente}\n"
        f"🕐 Hora: {pedido['hora']}\n"
        f"{icono} Tipo: {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger en local'}\n"
        f"📍 Dirección: {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}"
    )
    enviar_whatsapp(ADMIN_NUMBER, msg)


# ── ENVÍO WHATSAPP ────────────────────────────────────────────────────────────

def enviar_whatsapp(numero, mensaje):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": mensaje}}
    r = requests.post(url, headers=headers, json=data)
    print("META →", r.status_code, r.text)
    return r


def enviar_documento_whatsapp(numero, url_documento, nombre_archivo, caption=""):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    documento = {"link": url_documento, "filename": nombre_archivo}
    if caption:
        documento["caption"] = caption
    data = {"messaging_product": "whatsapp", "to": numero, "type": "document", "document": documento}
    r = requests.post(url, headers=headers, json=data)
    print("META (doc) →", r.status_code, r.text)
    return r


def enviar_menu_pdf(numero, rest_key):
    r = RESTAURANTES[rest_key]
    nombre_archivo = f"Menu-{r['nombre'].replace(' ', '-')}.pdf"
    ruta_pdf = os.path.join(STATIC_DIR, f"menu_{rest_key}.pdf")
    base_url = os.getenv("PANEL_URL", "").rstrip("/")
    if not os.path.exists(ruta_pdf) or not base_url:
        enviar_whatsapp(numero, "📋 Por ahora puedo contarte el menú aquí mismo, ¡pregúntame lo que quieras! 😊")
        return False
    url_pdf = f"{base_url}/static/menu_{rest_key}.pdf"
    enviar_documento_whatsapp(numero, url_pdf, nombre_archivo, caption=f"📋 Menú completo de {r['nombre']}")
    enviar_whatsapp(numero, f"📋 *Menú de {r['nombre']}:*\n{url_pdf}\n\n👉 Toca el link para verlo")
    return True


# ── COMANDOS ADMIN ────────────────────────────────────────────────────────────

def procesar_comando_admin(texto):
    t = texto.strip().lower()

    if t in ["ayuda", "help", "comandos"]:
        return (
            "🛠️ *Comandos admin:*\n\n"
            "*Restaurantes:*\n"
            "• abre las bravas → abre solo hoy\n"
            "• cierra escarabajo → cierra aunque sea horario\n"
            "• abre monaco\n\n"
            "*Por restaurante (agrega el nombre al final):*\n"
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
            "• ayuda → este mensaje"
        )

    if t in ["estado", "ver estado"]:
        lineas = ["📋 *Estado restaurantes:*\n"]
        for key, r in RESTAURANTES.items():
            abierto = "✅ Abierto" if esta_abierto(key) else "❌ Cerrado"
            forzado = " *(forzado hoy)*" if r["abierto_forzado"] and r["fecha_forzado"] == datetime.now(ZONA_HORARIA).date() else ""
            dom = "✅" if r["domicilio_activo"] else "❌"
            espera = f"{r['tiempo_espera']}min" if r["tiempo_espera"] else "—"
            desact = ", ".join(r["categorias_desactivadas"]) or "ninguna"
            notas_txt = ", ".join(r["notas"]) or "ninguna"
            lineas.append(
                f"*{r['nombre']}*\n"
                f"  {abierto}{forzado}\n"
                f"  🛵 Dom: {dom} | ⏱ Espera: {espera}\n"
                f"  ❌ Desactivados: {desact}\n"
                f"  📝 Notas: {notas_txt}"
            )
        return "\n\n".join(lineas)

    # Identificar restaurante en el texto
    rest_key = None
    for key, r in RESTAURANTES.items():
        nombre = r["nombre"].lower()
        if nombre in t or key.replace("_", " ") in t or key in t:
            rest_key = key
            break

    # ABRIR / CERRAR
    if t.startswith("abre ") or t.startswith("abrir "):
        if rest_key:
            RESTAURANTES[rest_key]["abierto_forzado"] = True
            RESTAURANTES[rest_key]["fecha_forzado"] = datetime.now(ZONA_HORARIA).date()
            return f"✅ *{RESTAURANTES[rest_key]['nombre']}* abierto manualmente por hoy. Mañana se cierra solo."
        return "⚠️ No encontré el restaurante. Usa: abre las bravas / escarabajo / monaco"

    if t.startswith("cierra ") or t.startswith("cerrar "):
        if rest_key:
            RESTAURANTES[rest_key]["abierto_forzado"] = False
            RESTAURANTES[rest_key]["fecha_forzado"] = None
            return f"✅ *{RESTAURANTES[rest_key]['nombre']}* cerrado manualmente."
        return "⚠️ No encontré el restaurante."

    if rest_key is None:
        return None

    r = RESTAURANTES[rest_key]
    nombre_display = r["nombre"]
    nombre_lower = nombre_display.lower()

    # DOMICILIO
    if "quita domicilio" in t or "desactiva domicilio" in t:
        r["domicilio_activo"] = False
        return f"✅ Domicilio desactivado en *{nombre_display}*."
    if "activa domicilio" in t or "pon domicilio" in t:
        r["domicilio_activo"] = True
        return f"✅ Domicilio activado en *{nombre_display}*."

    # TIEMPO DE ESPERA
    m = re.search(r"espera\s+(\d+)", t)
    if m:
        r["tiempo_espera"] = int(m.group(1))
        return f"✅ Espera de *{m.group(1)} min* en {nombre_display}."
    if "sin espera" in t or "quita espera" in t:
        r["tiempo_espera"] = None
        return f"✅ Espera eliminada en *{nombre_display}*."

    # NOTAS
    if "borra notas" in t or "quita notas" in t:
        r["notas"].clear()
        return f"✅ Notas borradas en *{nombre_display}*."
    if t.startswith("nota "):
        nota = t.replace("nota ", "").replace(nombre_lower, "").replace(rest_key.replace("_", " "), "").strip()
        r["notas"].append(nota)
        return f"✅ Nota en *{nombre_display}*: '{nota}'"

    # CATEGORÍAS
    if t.startswith("quita ") or t.startswith("desactiva "):
        palabra = re.sub(r"(quita|desactiva)\s+", "", t).replace(nombre_lower, "").replace(rest_key.replace("_", " "), "").strip()
        for cat in r["menu"]:
            if palabra in cat or cat in palabra:
                r["categorias_desactivadas"].add(cat)
                return f"✅ *{cat}* desactivado en {nombre_display}."
        return f"⚠️ No encontré '{palabra}' en {nombre_display}. Categorías: {', '.join(r['menu'].keys())}"

    if t.startswith("activa ") or t.startswith("pon "):
        palabra = re.sub(r"(activa|pon)\s+", "", t).replace(nombre_lower, "").replace(rest_key.replace("_", " "), "").strip()
        for cat in r["menu"]:
            if palabra in cat or cat in palabra:
                r["categorias_desactivadas"].discard(cat)
                return f"✅ *{cat}* activado en {nombre_display}."
        return f"⚠️ No encontré '{palabra}' en {nombre_display}."

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
.dir{font-size:.78rem;color:#888;margin-bottom:6px;word-break:break-word}
.resumen{background:#FFFBF2;padding:8px;border-radius:6px;font-size:.76rem;color:#444;max-height:100px;overflow-y:auto;border-left:3px solid #FFC107;white-space:pre-wrap;margin:6px 0}
.mods{background:#FFF6E0;border-left:3px solid #FF9800;padding:7px;border-radius:6px;margin:6px 0;font-size:.73rem}
.quejas-box{background:#FDECEA;border-left:3px solid #f44336;padding:7px;border-radius:6px;margin:6px 0;font-size:.73rem}
.est-lbl{text-align:center;font-size:.78rem;font-weight:700;padding:4px;border-radius:4px;margin:8px 0 5px}
.est-lbl.activo{color:#2E7D32}.est-lbl.preparando{color:#E65100}.est-lbl.enviado{color:#1565C0}.est-lbl.entregado{color:#6A1B9A}.est-lbl.cancelado{color:#C62828}
.ebts{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
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
  <button class="tab" data-tab="preparacion" onclick="cambiarTab('preparacion')">🍳 Preparando <span id="c-prep"></span></button>
  <button class="tab" data-tab="enviados" onclick="cambiarTab('enviados')">🛵 Enviados <span id="c-env"></span></button>
  <button class="tab" data-tab="entregados" onclick="cambiarTab('entregados')">✅ Entregados <span id="c-ent"></span></button>
</div>

<div id="grid" class="grid"></div>
<div id="empty" class="empty" style="display:none">No hay pedidos en esta pestaña 😊</div>
</div>

<script>
const pw = "{{PW}}";
let todos = [], tabActual = "todos";

async function cargarPedidos(){
  try{
    const r = await fetch(`/api/pedidos?pw=${encodeURIComponent(pw)}`);
    if(r.status===403){window.location.href='/panel';return;}
    const d = await r.json();
    todos = d.pedidos;
    render(); stats(); contadores(); ventasHoy();
  }catch(e){console.error(e);}
}

function esHoy(iso){
  try{
    const a=new Date(iso).toLocaleDateString('es-CO',{timeZone:'America/Bogota'});
    const b=new Date().toLocaleDateString('es-CO',{timeZone:'America/Bogota'});
    return a===b;
  }catch(e){return false;}
}

function extraerTotal(r){
  if(!r)return 0;
  const m=r.match(/Total:?\\s*\\$?\\s?([\\d.,]+)/i);
  if(!m)return 0;
  return parseInt(m[1].replace(/[.,]/g,''),10)||0;
}

function ventasHoy(){
  const hoy=todos.filter(p=>esHoy(p.hora_iso));
  const vend=hoy.filter(p=>p.estado!=='cancelado');
  const canc=hoy.filter(p=>p.estado==='cancelado');
  const total=vend.reduce((a,p)=>a+extraerTotal(p.resumen),0);
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

const EST_MAP={activo:'🆕 Activo',preparando:'🍳 Preparando',enviado:'🛵 Enviado',entregado:'✅ Entregado',cancelado:'❌ Cancelado'};
const BTNS=[
  {k:'activo',i:'🆕',c:'eb-activo'},{k:'preparando',i:'🍳',c:'eb-preparando'},
  {k:'enviado',i:'🛵',c:'eb-enviado'},{k:'entregado',i:'✅',c:'eb-entregado'},{k:'cancelado',i:'❌',c:'eb-cancelado'}
];

function render(){
  const g=document.getElementById('grid');
  const e=document.getElementById('empty');
  const lista=filtrar(tabActual);
  if(!lista.length){g.innerHTML='';e.style.display='block';return;}
  e.style.display='none';
  g.innerHTML=lista.map(p=>`
    <div class="card">
      <div class="pid">#${p.id}</div>
      <div class="rest-tag">🍽️ ${p.restaurante||'—'}</div>
      <div class="hora">🕐 ${p.hora}</div>
      <div class="tipo-tag">${p.tipo==='domicilio'?'🛵 Domicilio':'🏠 Recoger'}</div>
      <div class="cli">📱 ${p.numero}</div>
      <div class="dir">📍 ${p.direccion}</div>
      <div class="resumen">${p.resumen}</div>
      ${p.modificaciones&&p.modificaciones.length?`<div class="mods"><strong>📝 Modificaciones:</strong><br>${p.modificaciones.join('<br>')}</div>`:''}
      ${p.quejas&&p.quejas.length?`<div class="quejas-box"><strong>⚠️ Quejas:</strong><br>${p.quejas.join('<br>')}</div>`:''}
      <div class="est-lbl ${p.estado}">${EST_MAP[p.estado]||p.estado}</div>
      <div class="ebts">${BTNS.map(b=>`<button class="eb ${b.c} ${p.estado===b.k?'on':''}" title="${b.k}" onclick="cambiarEstado('${p.id}','${b.k}')">${b.i}</button>`).join('')}</div>
    </div>`).join('');
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
    return {"pedidos": list(reversed(pedidos))}

@app.post("/api/pedidos/{pedido_id}/estado")
async def cambiar_estado(pedido_id: str, request: Request):
    body = await request.json()
    if body.get("pw") != PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    nuevo = body.get("estado", "")
    if nuevo not in ["activo", "preparando", "enviado", "entregado", "cancelado"]:
        raise HTTPException(status_code=400)
    pedido = next((p for p in pedidos if p["id"] == pedido_id), None)
    if not pedido:
        raise HTTPException(status_code=404)

    anterior = pedido["estado"]
    pedido["estado"] = nuevo
    numero = pedido["numero"]

    if nuevo == "enviado" and anterior != "enviado":
        msg = (
            f"🛵 *¡Tu pedido va en camino!*\nPedido #{pedido['id']} hacia {pedido['direccion']}.\n¡Gracias por pedir en {pedido['restaurante']}! 🍔"
            if pedido["tipo"] == "domicilio"
            else f"✅ *¡Tu pedido está listo!*\nPedido #{pedido['id']} listo para recoger.\n¡Te esperamos en {pedido['restaurante']}! 🍔"
        )
        enviar_whatsapp(numero, msg)

    if nuevo == "entregado" and anterior != "entregado":
        enviar_whatsapp(numero, f"🙌 *¡Pedido entregado!* Esperamos que lo disfrutes.\n¡Gracias por elegir {pedido['restaurante']}! 😊")

    if nuevo == "cancelado" and anterior != "cancelado":
        enviar_whatsapp(numero, f"❌ *Pedido #{pedido['id']} cancelado.*\nSi tienes dudas contáctanos. ¡Hasta pronto! 🍔")

    return {"ok": True, "pedido": pedido}


# ── WEBHOOK ───────────────────────────────────────────────────────────────────

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

        # Solo texto
        if mensaje.get("type") != "text":
            numero = mensaje["from"]
            if numero != ADMIN_NUMBER:
                enviar_whatsapp(numero, "Por ahora solo puedo leer mensajes de texto 😊 Escríbeme tu pedido.")
            return {"status": "ok"}

        # Deduplicar
        message_id = mensaje.get("id", "")
        if message_id in mensajes_procesados:
            return {"status": "ok"}
        mensajes_procesados.add(message_id)
        if len(mensajes_procesados) > 500:
            ids = list(mensajes_procesados)
            mensajes_procesados.clear()
            mensajes_procesados.update(ids[-250:])

        numero = mensaje["from"]
        texto = mensaje["text"]["body"]
        texto_lower = texto.strip().lower()
        print(f"De {numero}: {texto}")

        # ── ADMIN ──
        if numero == ADMIN_NUMBER:
            resp_admin = procesar_comando_admin(texto)
            if resp_admin:
                enviar_whatsapp(numero, resp_admin)
                return {"status": "ok"}

        # ── PALABRAS CLAVE PARA VOLVER AL MENÚ DE RESTAURANTES ──
        if texto_lower in ["inicio", "volver", "restaurantes", "cambiar", "menu", "menú"]:
            cliente_restaurante.pop(numero, None)
            historial.pop(numero, None)
            clientes_eligiendo[numero] = True
            enviar_whatsapp(numero, lista_restaurantes())
            return {"status": "ok"}

        # ── CLIENTE SIN RESTAURANTE ELEGIDO ──
        if numero not in cliente_restaurante:
            clientes_eligiendo[numero] = True
            enviar_whatsapp(numero, lista_restaurantes())
            return {"status": "ok"}

        # ── CLIENTE ELIGIENDO RESTAURANTE ──
        if numero in clientes_eligiendo:
            keys = list(RESTAURANTES.keys())
            rest_key = None
            if texto_lower == "1": rest_key = keys[0]
            elif texto_lower == "2": rest_key = keys[1]
            elif texto_lower == "3": rest_key = keys[2]
            else:
                for k, r in RESTAURANTES.items():
                    if r["nombre"].lower() in texto_lower or k.replace("_", " ") in texto_lower:
                        rest_key = k
                        break

            if rest_key is None:
                enviar_whatsapp(numero, "Por favor responde *1*, *2* o *3* para elegir el restaurante 😊")
                return {"status": "ok"}

            if not esta_abierto(rest_key):
                r = RESTAURANTES[rest_key]
                enviar_whatsapp(numero,
                    f"😔 *{r['nombre']}* está cerrado ahora.\n"
                    f"Horario: {r['hora_inicio']}:00 – {r['hora_fin']}:00.\n\n"
                    + lista_restaurantes())
                return {"status": "ok"}

            cliente_restaurante[numero] = rest_key
            clientes_eligiendo.pop(numero, None)
            historial[numero] = []
            r = RESTAURANTES[rest_key]
            enviar_whatsapp(numero,
                f"¡Perfecto! Estás en *{r['nombre']}* 🎉\n"
                f"📍 {r['direccion']}\n\n"
                f"¿Qué deseas pedir? Puedes pedirme el menú o decirme directamente 😊\n\n"
                f"_(Escribe *restaurantes* para volver a elegir)_")
            return {"status": "ok"}

        # ── YA ELIGIÓ RESTAURANTE ──
        rest_key = cliente_restaurante[numero]

        # Verificar que siga abierto
        if not esta_abierto(rest_key) and numero != ADMIN_NUMBER:
            r = RESTAURANTES[rest_key]
            enviar_whatsapp(numero,
                f"😔 *{r['nombre']}* cerró por hoy. Horario: {r['hora_inicio']}:00–{r['hora_fin']}:00.\n"
                f"¡Gracias! Vuelve mañana 🙏")
            cliente_restaurante.pop(numero, None)
            return {"status": "ok"}

        # ─── ¿CLIENTE RESPONDIENDO "MODIFICAR O NUEVO"? ──────────────────────
        saltar_palabras_clave = False
        if numero in clientes_esperando_decision:
            mensaje_original = clientes_esperando_decision.pop(numero)
            quiere_modificar = any(p in texto_lower for p in ["modificar", "el mismo", "modificarlo", "ese pedido", "el actual", "actualizar"])
            quiere_nuevo = any(p in texto_lower for p in ["nuevo", "otro pedido", "uno nuevo", "aparte", "diferente"])

            if quiere_modificar:
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["modificaciones"].append(mensaje_original)
                    enviar_whatsapp(numero, "✅ Listo, hemos anotado ese cambio en tu pedido. ¡El equipo lo procesará! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER,
                        f"📝 *Pedido #{pedido['id']} modificado*\n"
                        f"🍽️ {pedido['restaurante']}\n"
                        f"📱 +{numero}\n────────────────\n{mensaje_original}")
                else:
                    enviar_whatsapp(numero, "Tu pedido anterior ya no está activo. ¿Quieres hacer uno nuevo? Cuéntame 😊")
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

        # ─── OPCIONES ESPECIALES DEL CLIENTE ─────────────────────────────────
        if not saltar_palabras_clave:

            # CANCELAR PEDIDO
            if any(p in texto_lower for p in ["cancelar pedido", "eliminar pedido", "quiero cancelar"]):
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["estado"] = "cancelado"
                    enviar_whatsapp(numero, f"❌ Tu pedido #{pedido['id']} ha sido cancelado.\nSi cambias de idea, ¡escríbenos! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ *Pedido #{pedido['id']} cancelado por cliente*\n+{numero}\n🍽️ {pedido['restaurante']}")
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo para cancelar. ¿Deseas hacer uno nuevo?")
                return {"status": "ok"}

            # MODIFICAR PEDIDO
            if any(p in texto_lower for p in ["modificar pedido", "cambiar plato", "quiero cambiar", "agregar algo"]):
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["modificaciones"].append(texto)
                    enviar_whatsapp(numero, "✅ Hemos recibido tu solicitud de cambio. ¡El equipo lo procesará! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER,
                        f"📝 *Pedido #{pedido['id']} modificado*\n"
                        f"🍽️ {pedido['restaurante']}\n📱 +{numero}\n────────────────\n{texto}")
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo. ¿Deseas hacer uno nuevo?")
                return {"status": "ok"}

            # QUEJAS
            if any(p in texto_lower for p in ["queja", "reclamación", "problema con mi pedido", "está mal", "no llegó"]):
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["quejas"].append(texto)
                    enviar_whatsapp(numero, "⚠️ Hemos recibido tu reclamación. Nuestro equipo se pondrá en contacto contigo pronto. ¡Disculpa! 😟")
                    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ *QUEJA — Pedido #{pedido['id']}*\n🍽️ {pedido['restaurante']}\n📱 +{numero}\n{texto}")
                else:
                    enviar_whatsapp(numero, "Cuéntanos qué pasó para ayudarte mejor 😊")
                return {"status": "ok"}

            # MENÚ PDF
            if any(p in texto_lower for p in ["pdf", "carta", "el menú", "el menu", "menú completo", "menu completo", "ver menú", "ver menu"]):
                enviado = enviar_menu_pdf(numero, rest_key)
                if enviado:
                    enviar_whatsapp(numero, "¿Qué te gustaría pedir? 😊")
                return {"status": "ok"}

            # ¿TIENE PEDIDO ACTIVO RECIENTE Y ESCRIBE ALGO AMBIGUO?
            pedido_activo = buscar_pedido_cliente(numero)
            if pedido_activo and pedido_es_reciente(pedido_activo):
                clientes_esperando_decision[numero] = texto
                enviar_whatsapp(numero,
                    f"👋 Ya tienes un pedido activo (#{pedido_activo['id']}) en *{pedido_activo['restaurante']}*.\n"
                    f"¿Quieres *modificar* ese pedido o hacer un *pedido nuevo*?\nResponde 'modificar' o 'nuevo' 😊")
                return {"status": "ok"}

        # ─── CONVERSACIÓN NORMAL CON CLAUDE ──────────────────────────────────
        if numero not in historial:
            historial[numero] = []
        historial[numero].append({"role": "user", "content": texto})

        ai = anthropic.Anthropic(api_key=CLAUDE_KEY)
        resp = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=build_system_prompt(rest_key),
            messages=historial[numero],
        )
        texto_respuesta = resp.content[0].text
        print(f"Respuesta: {texto_respuesta}")
        historial[numero].append({"role": "assistant", "content": texto_respuesta})

        if len(historial[numero]) > 30:
            historial[numero] = historial[numero][-30:]

        enviar_whatsapp(numero, texto_respuesta)

        # ── DETECTAR CIERRE DE PEDIDO ─────────────────────────────────────────
        palabras_cierre = ["en camino", "listo para recoger", "pasamos a preparar", "empezamos a preparar"]
        tiene_contexto = any(p in texto_respuesta.lower() for p in ["domicilio", "recoger", "local"])
        es_cierre = any(p in texto_respuesta.lower() for p in palabras_cierre)

        if es_cierre and tiene_contexto:
            resumen = texto_respuesta
            for msg in reversed(historial[numero]):
                if msg["role"] == "assistant" and "total" in msg["content"].lower() and "$" in msg["content"]:
                    resumen = msg["content"]
                    break
            pedido, es_nuevo = registrar_pedido(numero, resumen, texto_respuesta, rest_key)
            if es_nuevo:
                notificar_pedido_admin(numero, pedido)
            else:
                notificar_pedido_actualizado_admin(numero, pedido)

    except Exception:
        traceback.print_exc()

    return {"status": "ok"}
