from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import anthropic, requests, os, traceback, uuid
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz

load_dotenv()

app = FastAPI()

# Carpeta para archivos estáticos (aquí va el logo: static/logo.png)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
CLAUDE_KEY = os.getenv("CLAUDE_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "broaster2024")

ADMIN_NUMBER = "573167731698"
ZONA_HORARIA = pytz.timezone("America/Bogota")

historial = {}
mensajes_procesados = set()

# Si un cliente que YA tiene un pedido activo escribe de nuevo dentro de este
# intervalo, le preguntamos si quiere modificar ese pedido o hacer uno nuevo.
INTERVALO_CORTO_MINUTOS = 15
# numero -> texto del mensaje que generó la duda (mientras esperamos su respuesta)
clientes_esperando_decision = {}


def pedido_es_reciente(pedido, minutos=INTERVALO_CORTO_MINUTOS):
    """True si el pedido se creó/actualizó hace menos de X minutos."""
    try:
        hora_pedido = datetime.fromisoformat(pedido["hora_iso"])
        ahora = datetime.now(ZONA_HORARIA)
        return (ahora - hora_pedido) <= timedelta(minutes=minutos)
    except Exception:
        return False


# ── PEDIDOS ─────────────────────────────────────────────────────────────────
pedidos = []


def registrar_pedido(numero_cliente, resumen, confirmacion_bot):
    """Crea un pedido nuevo, o si el cliente ya tiene uno activo/preparando,
    actualiza ESE MISMO pedido en lugar de crear un duplicado.
    Devuelve (pedido, es_nuevo)."""
    es_domicilio = "camino" in confirmacion_bot.lower() or "domicilio" in confirmacion_bot.lower()
    tipo = "domicilio" if es_domicilio else "recoger"

    direccion = ""
    if es_domicilio:
        texto = confirmacion_bot.lower()
        if "domicilio a" in texto:
            inicio = confirmacion_bot.lower().index("domicilio a") + len("domicilio a")
            direccion = confirmacion_bot[inicio:].split(".")[0].strip()
        elif "a la dirección" in texto:
            inicio = confirmacion_bot.lower().index("a la dirección") + len("a la dirección")
            direccion = confirmacion_bot[inicio:].split(".")[0].strip()

    ahora = datetime.now(ZONA_HORARIA)

    # ¿El cliente ya tiene un pedido activo/preparando? Si sí, lo ACTUALIZAMOS
    # en vez de crear uno nuevo (evita duplicados cuando el cliente modifica
    # y vuelve a confirmar con el bot).
    existente = buscar_pedido_cliente(numero_cliente)
    if existente:
        existente["resumen"] = resumen
        existente["confirmacion"] = confirmacion_bot
        existente["direccion"] = direccion if direccion else existente["direccion"]
        existente["tipo"] = tipo
        existente["estado"] = "activo"
        existente["hora"] = ahora.strftime("%I:%M %p")
        existente["hora_iso"] = ahora.isoformat()
        return existente, False

    pedido = {
        "id": str(uuid.uuid4())[:8].upper(),
        "numero": numero_cliente,
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
    if len(pedidos) > 100:
        pedidos.pop(0)
    return pedido, True


def buscar_pedido_cliente(numero_cliente):
    """Busca el último pedido activo (o preparando) de un cliente."""
    for p in reversed(pedidos):
        if p["numero"] == numero_cliente and p["estado"] in ["activo", "preparando"]:
            return p
    return None


# ── MENÚ ─────────────────────────────────────────────────────────────────────
menu = {
    "sopas": "Sopas: Consomé, caldo de pollo con menudencias $7.000 / Consomé Especial, caldo con pollo desmechado $8.000 / Sopa de Pollo, sopa de pollo con presa $13.000",
    "varios": "Varios: Hamburguesa de pollo o de res + papas fritas $17.000 / Plato de Chorizo, 2 chorizos + papas fritas + ensalada $12.000 / Plato de Salchichas, papas fritas + ensalada $12.000 / Salchipapa Sencilla $10.000 / Salchipapa Doble $17.000 / Extras, porción de papa, arroz o ensalada $7.000 / Cajita Feliz, 1 presa + papas fritas + juguete + jugo $19.000",
    "bebidas": "Bebidas: Jugo 237ml $4.000 / Jarra de Limonada $16.000 / Media Jarra de Limonada $9.000 / Jugo Natural en Leche $7.000 / Jugo Natural en Agua $6.000 / Jugo del Valle 250ml $3.000 / Jugo Hit 500ml $5.000 / Jugo Hit 946ml $8.000 / Vaso Limonada Natural $4.000 / Malteada $7.000 / Gaseosa 1 Litro $8.000 / Gaseosa Familiar $12.000 / Gaseosa Personal $4.000 / Pony Malta Litro $8.000 / Pony Malta 350ml $4.000 / Té 550ml $5.000 / Té Litro $8.000 / Cerveza en Lata $8.000 / Agua Botella $4.000 / Agua Guitig $5.000 / Agua H2O $4.000",
    "aves": "Aves: 1 Pollo, 8 presas + papas fritas $54.000 / Medio Pollo, 4 presas + papas fritas $27.000 / Senior, 3 presas + papas fritas $23.000 / Estándar, 2 presas + papas fritas $14.000 / Junior, 1 presa + papas fritas $8.000 / Arroz con Pollo, papas fritas + ensalada $16.000 / Estándar con Arroz, 2 presas + papas fritas + ensalada $19.000 / Junior con Arroz, 1 presa + papas fritas + ensalada $17.000 / Plato Mixto, 1 presa + 1 chorizo + 1 salchicha + arroz + papas fritas + ensalada $25.000 / Pollo Picado, papas fritas + patacón + crispeta $80.000 / Medio Pollo Picado, papas fritas + patacón + crispeta $60.000",
    "especiales": "Especiales: Picada Broaster, pollo + chorizo + salchicha + papas fritas + patacón + crispeta $90.000 / Media Picada Broaster $70.000 / Picada Personal $30.000 / Alitas B.B.Q., papas fritas + ensalada $25.000 / Costillas B.B.Q., papas fritas + ensalada $26.000 / Media Costillas B.B.Q., papas fritas + ensalada $17.000 / Crispetas de Pollo, papas fritas $18.000 / Chuleta de Cerdo, papas fritas + arroz + ensalada $25.000 / Media Chuleta de Cerdo, papas fritas + arroz + ensalada $16.000 / Chuleta de Pollo, papas fritas + arroz + ensalada $25.000 / Media Chuleta de Pollo, papas fritas + arroz + ensalada $16.000 / Pechuga a la Plancha, papas fritas + arroz + ensalada $25.000",
    "combos": "Combos: Combo 1, 12 presas + papas fritas + arroz + ensalada + gaseosa 1L $100.000 / Combo 2, 8 presas + papas fritas + arroz + ensalada + gaseosa 1L $80.000 / Combo 3, 4 presas + papas fritas + ensalada + media jarra de limonada $50.000 / Combo 4, 3 presas + papas fritas + ensalada + limonada $45.000 / Combo 5, consomé + 2 presas + arroz + papas fritas + ensalada + limonada $24.000 / Combo 6, consomé + 1 presa + papas fritas + arroz + ensalada + limonada $21.000",
}

categorias_desactivadas = set()
notas_admin = []
domicilio_activo = True
tiempo_espera = None


def esta_abierto():
    ahora = datetime.now(ZONA_HORARIA)
    return 13 <= ahora.hour < 23


def build_system_prompt():
    menu_activo = [v for k, v in menu.items() if k not in categorias_desactivadas]
    notas = "\nNOTAS ESPECIALES DE HOY:\n- " + "\n- ".join(notas_admin) if notas_admin else ""
    espera_txt = f"\nTIEMPO DE ESPERA ACTUAL: {tiempo_espera} minutos. Infórmalo al confirmar." if tiempo_espera else ""
    domicilio_txt = (
        "Sí. Costo: $6.000. Sin mínimo. Horario igual al de atención."
        if domicilio_activo else
        "No disponible. Solo atención en local."
    )

    return f"""Eres el asistente virtual de Broaster King Ipiales, restaurante de especialidades en pollo broaster, ubicado en Cra. 7ma #12-05, Ipiales. Teléfonos: 602 773 3214 - 773 8827 / Cel: 312 861 4485.
HORARIO: 1:00pm – 11:00pm
DOMICILIO: {domicilio_txt}
MÉTODOS DE PAGO: Nequi, Daviplata, transferencia, efectivo.
MENÚ:
{chr(10).join(menu_activo)}
{notas}{espera_txt}

INSTRUCCIONES CRÍTICAS PARA MANEJO DEL PEDIDO:
- Habla amigable y natural como empleado real.
- Acumula TODOS los productos que el cliente pide sin mostrar resumen parcial.
- NUNCA muestres resumen ni total hasta que el cliente diga "es todo", "eso sería", "listo", "ya es todo", "nada más" o similar.
- Solo entonces muestra el resumen completo con todos los productos y el total.

INSTRUCCIONES SOBRE LA DIRECCIÓN (MUY IMPORTANTE):
- Si el cliente YA mencionó en cualquier parte de la conversación —incluso desde su primer mensaje— un lugar de entrega (edificio, casa, barrio, calle, conjunto residencial, punto de referencia — ejemplo: "para el edificio IPK", "en mi casa del barrio X", "a la calle 5"), eso significa que el pedido es DOMICILIO y esa es la dirección. NO preguntes "¿es domicilio o para recoger?".
- En ese caso, antes de cerrar el pedido, confirma esa dirección y pregunta si hay algún detalle adicional, así: "Perfecto, te lo enviamos al Edificio IPK 🛵 ¿Hay algún detalle adicional (apartamento, torre, punto de referencia) o así está bien?". Espera la respuesta del cliente.
- Una vez el cliente confirme (diga "así está bien", "correcto", o dé un detalle extra como el apartamento/torre), NO vuelvas a preguntar la dirección. Cierra el pedido con exactamente: "Perfecto, domicilio a [dirección + detalle si lo dio]. Tu pedido ya está en camino 🛵"
- Solo si el cliente NO ha mencionado ningún lugar de entrega, pregunta: "¿Es para domicilio o para recoger en el local?". Si responde domicilio y aún no diste dirección, ahí sí pídela completa.
- Si es para recoger, confirma con: "Perfecto, tu pedido estará listo para recoger en Cra. 7ma #12-05, Ipiales 🍔"
- No repitas el resumen ni el total después de confirmar.
- No inventes productos ni precios. Si no sabes algo, sugiere llamar.
- Si quiere hablar con persona real, dile que lo comunicas con el equipo.
- Responde siempre en español. Sé conciso."""


def enviar_whatsapp(numero, mensaje):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": mensaje}}
    r = requests.post(url, headers=headers, json=data)
    print("META →", r.status_code, r.text)
    return r


def notificar_pedido_admin(numero_cliente, pedido):
    """Notifica al admin por WhatsApp con los detalles de un pedido NUEVO."""
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    ahora = datetime.now(ZONA_HORARIA).strftime("%I:%M %p")
    mensaje = (
        f"🛎️ *Pedido #{pedido['id']}*\n"
        f"📱 Cliente: +{numero_cliente}\n"
        f"🕐 Hora: {ahora}\n"
        f"{icono} Tipo: {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger en local'}\n"
        f"📍 Dirección: {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}\n"
        f"────────────────\n"
        f"👉 Ver panel: {os.getenv('PANEL_URL', 'Tu URL de Railway')}/panel"
    )
    enviar_whatsapp(ADMIN_NUMBER, mensaje)


def notificar_pedido_actualizado_admin(numero_cliente, pedido):
    """Notifica al admin que un pedido EXISTENTE fue actualizado (no es nuevo)."""
    icono = "🛵" if pedido["tipo"] == "domicilio" else "🏠"
    ahora = datetime.now(ZONA_HORARIA).strftime("%I:%M %p")
    mensaje = (
        f"🔄 *Pedido #{pedido['id']} actualizado*\n"
        f"📱 Cliente: +{numero_cliente}\n"
        f"🕐 Hora: {ahora}\n"
        f"{icono} Tipo: {'Domicilio' if pedido['tipo'] == 'domicilio' else 'Recoger en local'}\n"
        f"📍 Dirección: {pedido['direccion']}\n"
        f"────────────────\n"
        f"{pedido['resumen']}\n"
        f"────────────────\n"
        f"👉 Ver panel: {os.getenv('PANEL_URL', 'Tu URL de Railway')}/panel"
    )
    enviar_whatsapp(ADMIN_NUMBER, mensaje)


def procesar_comando_admin(texto):
    global domicilio_activo, tiempo_espera
    t = texto.strip().lower()

    if t in ["quita domicilio", "desactiva domicilio", "sin domicilio", "no hay domicilio"]:
        domicilio_activo = False
        return "✅ Domicilio desactivado."
    if t in ["activa domicilio", "pon domicilio", "hay domicilio"]:
        domicilio_activo = True
        return "✅ Domicilio activado."

    if t.startswith("espera "):
        minutos = t.replace("espera ", "").strip()
        if minutos.isdigit():
            tiempo_espera = int(minutos)
            return f"✅ Tiempo de espera: *{minutos} minutos*."
        return "⚠️ Formato: *espera 30*"
    if t in ["sin espera", "quita espera", "espera normal"]:
        tiempo_espera = None
        return "✅ Tiempo de espera eliminado."

    if t.startswith("quita ") or t.startswith("desactiva "):
        palabra = t.replace("quita ", "").replace("desactiva ", "").strip()
        for key in menu:
            if palabra in key or key in palabra:
                categorias_desactivadas.add(key)
                return f"✅ *{key.capitalize()}* desactivado."
        return f"⚠️ No encontré '{palabra}'."

    if t.startswith("activa ") or t.startswith("pon "):
        palabra = t.replace("activa ", "").replace("pon ", "").strip()
        for key in menu:
            if palabra in key or key in palabra:
                categorias_desactivadas.discard(key)
                return f"✅ *{key.capitalize()}* activado."
        return f"⚠️ No encontré '{palabra}'."

    if t.startswith("nota ") or t.startswith("agrega nota "):
        nota = t.replace("nota ", "").replace("agrega nota ", "").strip()
        notas_admin.append(nota)
        return f"✅ Nota: '{nota}'"
    if t in ["borra notas", "borrar notas", "sin notas", "quita notas"]:
        notas_admin.clear()
        return "✅ Notas borradas."

    return None


# ── PANEL: PÁGINA DE LOGIN (solo se ve si la contraseña no se ha validado) ──

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Broaster King Ipiales - Login</title>
<style>
  *{box-sizing:border-box}
  body{background:#FFF8E7;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#2b2b2b}
  .box{background:#fff;border:1px solid #FFE2A8;box-shadow:0 8px 28px rgba(0,0,0,0.08);padding:34px 30px;border-radius:16px;text-align:center;width:90%;max-width:360px}
  .logo-box{width:72px;height:72px;margin:0 auto 14px;border-radius:16px;overflow:hidden;display:flex;align-items:center;justify-content:center;background:#FFF3D6;border:2px dashed #FFC107}
  .logo-box img{width:100%;height:100%;object-fit:contain;display:none}
  .logo-box .logo-fallback{font-size:0.6rem;font-weight:700;letter-spacing:.5px;color:#C98A00}
  h1{color:#2b2b2b;margin-bottom:2px;font-size:1.3rem;letter-spacing:.3px}
  h1 span{color:#F57C00}
  p{color:#9a8a6b;margin-bottom:22px;font-size:0.88rem}
  input{width:100%;padding:13px;background:#FFFBF2;border:1px solid #FFE2A8;border-radius:10px;color:#2b2b2b;font-size:1rem;outline:none;margin-bottom:14px}
  input:focus{border-color:#FFC107;box-shadow:0 0 0 3px rgba(255,193,7,0.18)}
  button{width:100%;padding:13px;background:linear-gradient(135deg,#FFC107,#F57C00);border:none;border-radius:10px;color:#1a1a1a;font-weight:700;font-size:1rem;cursor:pointer;letter-spacing:.3px}
  button:hover{filter:brightness(1.05)}
  .error{color:#f44336;font-size:0.85rem;margin-top:-5px;margin-bottom:10px;display:none}
</style></head><body>
<div class="box">
  <div class="logo-box">
    <img id="logo-img" src="/static/logo.png" alt="Logo">
    <span class="logo-fallback" id="logo-fallback">LOGO</span>
  </div>
  <h1>🍗 BROASTER <span>KING</span></h1>
  <p>Panel de pedidos · Ipiales</p>
  <div class="error" id="error-msg">❌ Contraseña incorrecta</div>
  <form id="login-form" autocomplete="on">
    <input type="password" id="pw" name="password" placeholder="Contraseña" autocomplete="current-password" autofocus>
    <button type="submit">Entrar</button>
  </form>
</div>
<script>
document.getElementById('login-form').addEventListener('submit', function(e){
    e.preventDefault();
    const pw = document.getElementById('pw').value;
    window.location.href = '/panel?pw=' + encodeURIComponent(pw);
});
const logoImg = document.getElementById('logo-img');
logoImg.onload = () => { logoImg.style.display = 'block'; document.getElementById('logo-fallback').style.display = 'none'; };
logoImg.onerror = () => { logoImg.style.display = 'none'; };
</script>
</body></html>"""


# ── PANEL: DASHBOARD (se sirve solo cuando la contraseña en la URL es correcta) ──

PANEL_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel de Pedidos - Broaster King Ipiales</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bk-amarillo: #FFC107;
            --bk-amarillo-fuerte: #FFA000;
            --bk-naranja: #F57C00;
            --bk-negro: #222018;
            --bk-crema: #FFFBF2;
            --bk-crema-borde: #FFE2A8;
            --bk-gris: #8a7f6a;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--bk-crema); color: var(--bk-negro); padding: 20px; min-height: 100vh; }
        .container { max-width: 1300px; margin: 0 auto; }

        header { margin-bottom: 20px; background: linear-gradient(135deg, var(--bk-amarillo), var(--bk-naranja)); border-radius: 14px; padding: 16px 20px; box-shadow: 0 4px 16px rgba(245,124,0,0.25); }
        .header-top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
        .header-titulo { display: flex; align-items: center; gap: 12px; }
        .logo-box { width: 48px; height: 48px; border-radius: 12px; background: rgba(255,255,255,0.55); border: 2px dashed rgba(34,32,24,0.35); display: flex; align-items: center; justify-content: center; overflow: hidden; flex-shrink: 0; }
        .logo-box img { width: 100%; height: 100%; object-fit: contain; display: none; }
        .logo-box .logo-fallback { font-size: 0.55rem; font-weight: 700; color: rgba(34,32,24,0.55); }
        h1 { font-size: 1.4rem; color: var(--bk-negro); letter-spacing: .3px; line-height: 1.2; }
        h1 .sub { display: block; font-size: 0.72rem; font-weight: 400; color: rgba(34,32,24,0.65); }
        .header-buttons { display: flex; gap: 10px; }
        .btn-refrescar { background: var(--bk-negro); color: #fff; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 0.85rem; }
        .btn-refrescar:hover { background: #3a362c; }
        .btn-refrescar.girando .icono-refresh { display: inline-block; animation: girar 0.6s linear; }
        @keyframes girar { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        .logout-btn { background: rgba(34,32,24,0.85); color: white; padding: 10px 18px; border-radius: 8px; border: none; cursor: pointer; font-weight: bold; font-size: 0.85rem; }
        .logout-btn:hover { background: var(--bk-negro); }
        .header-info { display: flex; gap: 18px; margin-top: 12px; font-size: 0.82rem; color: rgba(34,32,24,0.75); flex-wrap: wrap; font-weight: 600; }

        /* RESUMEN DE VENTAS DEL DÍA */
        .ventas-dia { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 18px 0; }
        .venta-card { background: #fff; border-radius: 12px; padding: 14px 16px; text-align: center; border: 1px solid var(--bk-crema-borde); }
        .venta-card .venta-numero { display: block; font-size: 1.5rem; font-weight: bold; color: var(--bk-negro); }
        .venta-card .venta-label { display: block; font-size: 0.75rem; color: var(--bk-gris); margin-top: 4px; }
        .venta-card.venta-total { background: var(--bk-negro); border-color: var(--bk-negro); }
        .venta-card.venta-total .venta-numero { color: var(--bk-amarillo); font-size: 1.7rem; }
        .venta-card.venta-total .venta-label { color: rgba(255,255,255,0.65); }
        .venta-card.venta-cancelados .venta-numero { color: #d32f2f; }

        /* PESTAÑAS */
        .tabs { display: flex; gap: 8px; margin: 18px 0; flex-wrap: wrap; }
        .tab-btn { background: #fff; color: var(--bk-gris); border: 1px solid var(--bk-crema-borde); padding: 9px 16px; border-radius: 20px; cursor: pointer; font-size: 0.85rem; font-weight: bold; transition: all .15s; }
        .tab-btn:hover { border-color: var(--bk-amarillo-fuerte); color: var(--bk-negro); }
        .tab-btn.tab-activo { background: linear-gradient(135deg, var(--bk-amarillo), var(--bk-naranja)); color: var(--bk-negro); border-color: var(--bk-naranja); }

        .pedidos-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 18px; }
        .pedido-card { background: #fff; border-left: 4px solid var(--bk-naranja); border-radius: 10px; padding: 16px; box-shadow: 0 2px 10px rgba(34,32,24,0.07); }
        .pedido-id { font-size: 1.15rem; font-weight: bold; color: var(--bk-naranja); }
        .pedido-hora { font-size: 0.8rem; color: var(--bk-gris); margin-top: 2px; }
        .pedido-tipo { display: inline-block; background: var(--bk-amarillo); color: var(--bk-negro); padding: 3px 9px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; margin-top: 8px; }
        .pedido-cliente { font-size: 0.85rem; color: #555; margin: 8px 0 2px; }
        .pedido-direccion { font-size: 0.8rem; color: var(--bk-gris); margin: 2px 0 8px; word-break: break-word; }
        .pedido-resumen { background: var(--bk-crema); padding: 8px; border-radius: 6px; font-size: 0.78rem; color: #444; margin: 8px 0; max-height: 110px; overflow-y: auto; border-left: 3px solid var(--bk-amarillo); white-space: pre-wrap; }
        .modificaciones, .quejas { padding: 8px; border-radius: 6px; margin: 8px 0; font-size: 0.75rem; color: #444; }
        .modificaciones { background: #FFF6E0; border-left: 3px solid #FF9800; }
        .quejas { background: #FDECEA; border-left: 3px solid #f44336; }

        /* ESTADO: solo texto + botones, sin lista desplegable */
        .estado-label { text-align: center; font-size: 0.8rem; font-weight: bold; margin: 10px 0 6px; padding: 4px; border-radius: 4px; }
        .estado-label.activo     { color: #2E7D32; }
        .estado-label.preparando { color: #E65100; }
        .estado-label.enviado    { color: #1565C0; }
        .estado-label.entregado  { color: #6A1B9A; }
        .estado-label.cancelado  { color: #C62828; }

        .estado-botones { display: grid; grid-template-columns: repeat(5, 1fr); gap: 5px; }
        .eb { padding: 9px 0; border: none; border-radius: 6px; cursor: pointer; font-size: 1rem; background: var(--bk-crema); color: #b0a888; opacity: 0.7; transition: all .15s; }
        .eb:hover { opacity: 1; }
        .eb.eb-on { opacity: 1; }
        .eb-activo.eb-on     { background: #4CAF50; color: #fff; }
        .eb-preparando.eb-on { background: #FF9800; color: #fff; }
        .eb-enviado.eb-on    { background: #2196F3; color: #fff; }
        .eb-entregado.eb-on  { background: #9C27B0; color: #fff; }
        .eb-cancelado.eb-on  { background: #f44336; color: #fff; }

        .empty-state { text-align: center; padding: 60px 20px; color: var(--bk-gris); font-size: 1rem; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div class="header-top">
            <div class="header-titulo">
                <div class="logo-box">
                    <img id="logo-img" src="/static/logo.png" alt="Logo">
                    <span class="logo-fallback" id="logo-fallback">LOGO</span>
                </div>
                <h1>🍗 BROASTER KING<span class="sub">Panel de Pedidos · Ipiales</span></h1>
            </div>
            <div class="header-buttons">
                <button class="btn-refrescar" id="btn-refrescar" onclick="actualizarTodo()"><span class="icono-refresh">🔄</span> Actualizar todo</button>
                <button class="logout-btn" onclick="logout()">Cerrar sesión</button>
            </div>
        </div>
        <div class="header-info">
            <span id="tiempo-actual">🕐 --:--</span>
            <span id="total-pedidos">📊 Total: 0</span>
            <span id="pedidos-activos">⚡ Activos: 0</span>
        </div>
    </header>

    <div class="ventas-dia">
        <div class="venta-card">
            <span class="venta-numero" id="venta-cantidad">0</span>
            <span class="venta-label">📦 Pedidos vendidos hoy</span>
        </div>
        <div class="venta-card venta-total">
            <span class="venta-numero" id="venta-total">$0</span>
            <span class="venta-label">💰 Total vendido hoy</span>
        </div>
        <div class="venta-card venta-cancelados">
            <span class="venta-numero" id="venta-cancelados">0</span>
            <span class="venta-label">❌ Cancelados hoy</span>
        </div>
    </div>

    <div class="tabs">
        <button class="tab-btn tab-activo" data-tab="todos" onclick="cambiarTab('todos')">📋 Todos <span id="cnt-todos"></span></button>
        <button class="tab-btn" data-tab="preparacion" onclick="cambiarTab('preparacion')">🍳 En preparación <span id="cnt-preparacion"></span></button>
        <button class="tab-btn" data-tab="enviados" onclick="cambiarTab('enviados')">🛵 Enviados <span id="cnt-enviados"></span></button>
        <button class="tab-btn" data-tab="entregados" onclick="cambiarTab('entregados')">✅ Entregados <span id="cnt-entregados"></span></button>
    </div>

    <div id="pedidos-container" class="pedidos-grid"></div>
    <div id="empty-state" class="empty-state" style="display:none;">No hay pedidos en esta pestaña</div>
</div>

<script>
const password = "{{PANEL_PASSWORD}}";
let pedidosActuales = [];
let tabActual = 'todos';

// Muestra el logo si ya subiste static/logo.png; si no existe, deja el placeholder "LOGO"
const logoImg = document.getElementById('logo-img');
logoImg.onload = () => { logoImg.style.display = 'block'; document.getElementById('logo-fallback').style.display = 'none'; };
logoImg.onerror = () => { logoImg.style.display = 'none'; };

function logout() {
    window.location.href = '/panel';
}

async function cargarPedidos() {
    try {
        const response = await fetch(`/api/pedidos?pw=${encodeURIComponent(password)}`);
        if (!response.ok) {
            if (response.status === 403) { logout(); return; }
            throw new Error('HTTP ' + response.status);
        }
        const data = await response.json();
        pedidosActuales = data.pedidos;
        renderizarPedidos();
        actualizarStats(pedidosActuales);
        actualizarContadoresTabs(pedidosActuales);
        actualizarVentasHoy(pedidosActuales);
    } catch (error) {
        console.error('Error al cargar pedidos:', error);
    }
}

function formatearPesos(numero) {
    return '$' + Math.round(numero).toLocaleString('es-CO');
}

function extraerTotalPedido(resumen) {
    // Busca un patrón como "Total: $16.000" dentro del resumen del pedido
    if (!resumen) return 0;
    const match = resumen.match(/Total:?\\s*\\$?\\s?([\\d.,]+)/i);
    if (!match) return 0;
    const limpio = match[1].replace(/[.,]/g, '');
    return parseInt(limpio, 10) || 0;
}

function esDeHoy(horaIso) {
    try {
        const fechaPedido = new Date(horaIso).toLocaleDateString('es-CO', { timeZone: 'America/Bogota' });
        const hoy = new Date().toLocaleDateString('es-CO', { timeZone: 'America/Bogota' });
        return fechaPedido === hoy;
    } catch (e) {
        return false;
    }
}

function actualizarVentasHoy(pedidos) {
    const pedidosHoy = pedidos.filter(p => esDeHoy(p.hora_iso));
    const vendidosHoy = pedidosHoy.filter(p => p.estado !== 'cancelado');
    const canceladosHoy = pedidosHoy.filter(p => p.estado === 'cancelado');

    const totalVentas = vendidosHoy.reduce((acc, p) => acc + extraerTotalPedido(p.resumen), 0);

    document.getElementById('venta-cantidad').textContent = vendidosHoy.length;
    document.getElementById('venta-total').textContent = formatearPesos(totalVentas);
    document.getElementById('venta-cancelados').textContent = canceladosHoy.length;
}

function actualizarTodo() {
    const btn = document.getElementById('btn-refrescar');
    btn.classList.add('girando');
    cargarPedidos().finally(() => {
        setTimeout(() => btn.classList.remove('girando'), 500);
    });
}

function cambiarTab(tab) {
    tabActual = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-activo'));
    document.querySelector(`.tab-btn[data-tab="${tab}"]`).classList.add('tab-activo');
    renderizarPedidos();
}

function filtrarPorTab(pedidos, tab) {
    if (tab === 'preparacion') return pedidos.filter(p => p.estado === 'activo' || p.estado === 'preparando');
    if (tab === 'enviados') return pedidos.filter(p => p.estado === 'enviado');
    if (tab === 'entregados') return pedidos.filter(p => p.estado === 'entregado');
    return pedidos; // todos
}

function actualizarContadoresTabs(pedidos) {
    document.getElementById('cnt-todos').textContent = `(${pedidos.length})`;
    document.getElementById('cnt-preparacion').textContent = `(${pedidos.filter(p => p.estado === 'activo' || p.estado === 'preparando').length})`;
    document.getElementById('cnt-enviados').textContent = `(${pedidos.filter(p => p.estado === 'enviado').length})`;
    document.getElementById('cnt-entregados').textContent = `(${pedidos.filter(p => p.estado === 'entregado').length})`;
}

function etiquetaEstado(estado) {
    const mapa = {
        activo: '🆕 Activo', preparando: '🍳 Preparando', enviado: '🛵 Enviado',
        entregado: '✅ Entregado', cancelado: '❌ Cancelado',
    };
    return mapa[estado] || estado;
}

function botonesEstado(estado, id) {
    const estados = [
        { key: 'activo', icon: '🆕', clase: 'eb-activo' },
        { key: 'preparando', icon: '🍳', clase: 'eb-preparando' },
        { key: 'enviado', icon: '🛵', clase: 'eb-enviado' },
        { key: 'entregado', icon: '✅', clase: 'eb-entregado' },
        { key: 'cancelado', icon: '❌', clase: 'eb-cancelado' },
    ];
    return estados.map(e => `
        <button class="eb ${e.clase} ${estado === e.key ? 'eb-on' : ''}"
                title="${e.key}"
                onclick="cambiarEstado('${id}','${e.key}')">${e.icon}</button>
    `).join('');
}

function renderizarPedidos() {
    const container = document.getElementById('pedidos-container');
    const emptyState = document.getElementById('empty-state');
    const pedidos = filtrarPorTab(pedidosActuales, tabActual);

    if (pedidos.length === 0) {
        container.innerHTML = '';
        emptyState.style.display = 'block';
        return;
    }
    emptyState.style.display = 'none';

    container.innerHTML = pedidos.map(p => `
        <div class="pedido-card">
            <div class="pedido-id">#${p.id}</div>
            <div class="pedido-hora">🕐 ${p.hora}</div>
            <div class="pedido-tipo">${p.tipo === 'domicilio' ? '🛵 Domicilio' : '🏠 Recoger'}</div>
            <div class="pedido-cliente">📱 ${p.numero}</div>
            <div class="pedido-direccion">📍 ${p.direccion}</div>
            <div class="pedido-resumen">${p.resumen}</div>

            ${p.modificaciones && p.modificaciones.length > 0 ? `
                <div class="modificaciones"><strong>📝 Modificaciones del cliente:</strong><br>${p.modificaciones.join('<br>')}</div>
            ` : ''}

            ${p.quejas && p.quejas.length > 0 ? `
                <div class="quejas"><strong>⚠️ Quejas:</strong><br>${p.quejas.join('<br>')}</div>
            ` : ''}

            <div class="estado-label ${p.estado}">${etiquetaEstado(p.estado)}</div>
            <div class="estado-botones">${botonesEstado(p.estado, p.id)}</div>
        </div>
    `).join('');
}

async function cambiarEstado(pedidoId, nuevoEstado) {
    try {
        const response = await fetch(`/api/pedidos/${pedidoId}/estado`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pw: password, estado: nuevoEstado })
        });
        if (!response.ok) throw new Error('Error al cambiar estado');
        cargarPedidos();
    } catch (error) {
        alert('Error al cambiar estado');
    }
}

function actualizarStats(pedidos) {
    const ahora = new Date().toLocaleTimeString('es-CO', {hour: '2-digit', minute:'2-digit'});
    document.getElementById('tiempo-actual').textContent = `🕐 ${ahora}`;
    document.getElementById('total-pedidos').textContent = `📊 Total: ${pedidos.length}`;
    const activos = pedidos.filter(p => p.estado === 'activo' || p.estado === 'preparando').length;
    document.getElementById('pedidos-activos').textContent = `⚡ Activos: ${activos}`;
}

// Carga inicial inmediata (ya estamos autenticados, la contraseña viene embebida)
cargarPedidos();
// Recarga automática cada 6 segundos
setInterval(cargarPedidos, 6000);
</script>
</body>
</html>
"""


@app.get("/")
async def raiz():
    return HTMLResponse("<html><body><h1>Bot de Broaster King Ipiales</h1><p>Sistema operativo. Accede a /panel para ver pedidos.</p></body></html>")


@app.get("/panel")
async def panel(pw: str = ""):
    # Si la contraseña es correcta, mostramos el dashboard YA AUTENTICADO
    # (la contraseña queda embebida en el JS, no se vuelve a pedir).
    if pw and pw == PANEL_PASSWORD:
        html = PANEL_HTML.replace("{{PANEL_PASSWORD}}", PANEL_PASSWORD)
        return HTMLResponse(html)

    # Si no hay contraseña o es incorrecta, mostramos el login (una sola vez)
    return HTMLResponse(LOGIN_HTML)


@app.get("/api/pedidos")
async def api_pedidos(pw: str = ""):
    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403, detail="No autorizado")
    return {"pedidos": list(reversed(pedidos))}


@app.post("/api/pedidos/{pedido_id}/estado")
async def cambiar_estado(pedido_id: str, request: Request):
    body = await request.json()
    pw = body.get("pw", "")
    nuevo_estado = body.get("estado", "")

    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403, detail="No autorizado")

    estados_validos = ["activo", "preparando", "enviado", "entregado", "cancelado"]
    if nuevo_estado not in estados_validos:
        raise HTTPException(status_code=400, detail="Estado inválido")

    pedido = next((p for p in pedidos if p["id"] == pedido_id), None)
    if not pedido:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    estado_anterior = pedido["estado"]
    pedido["estado"] = nuevo_estado

    if nuevo_estado == "enviado" and estado_anterior != "enviado":
        if pedido["tipo"] == "domicilio":
            msg = (
                f"🛵 *¡Tu pedido va en camino!*\n"
                f"Pedido #{pedido['id']} ha salido hacia {pedido['direccion']}.\n"
                f"¡Gracias por pedir en Broaster King! 🍔"
            )
        else:
            msg = (
                f"✅ *¡Tu pedido está listo!*\n"
                f"Pedido #{pedido['id']} está listo para recoger en Cra. 7ma #12-05, Ipiales.\n"
                f"¡Te esperamos! 🍔"
            )
        enviar_whatsapp(pedido["numero"], msg)

    if nuevo_estado == "entregado" and estado_anterior != "entregado":
        enviar_whatsapp(
            pedido["numero"],
            f"🙌 *¡Pedido entregado!* Esperamos que lo disfrutes.\n"
            f"¡Gracias por elegirnos! Vuelve pronto 😊"
        )

    if nuevo_estado == "cancelado" and estado_anterior != "cancelado":
        enviar_whatsapp(
            pedido["numero"],
            f"❌ *Pedido #{pedido['id']} cancelado.*\n"
            f"Si tienes dudas, contáctanos.\n"
            f"¡Esperamos verte pronto! 🍔"
        )

    return {"ok": True, "pedido": pedido}


# ── WEBHOOK ──────────────────────────────────────────────────────────────────

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

        if mensaje.get("type") == "location":
            numero = mensaje["from"]
            loc = mensaje["location"]
            lat, lng = loc["latitude"], loc["longitude"]
            maps_link = f"https://maps.google.com/?q={lat},{lng}"
            for p in reversed(pedidos):
                if p["numero"] == numero and p["estado"] == "activo":
                    p["direccion"] = maps_link
                    break
            enviar_whatsapp(numero, "📍 ¡Ubicación recibida! Ya sabemos dónde entregarte. Tu pedido va en camino 🛵")
            enviar_whatsapp(ADMIN_NUMBER, f"📍 Ubicación de +{numero}:\n{maps_link}")
            return {"status": "ok"}

        if mensaje.get("type") != "text":
            numero = mensaje["from"]
            if numero != ADMIN_NUMBER:
                enviar_whatsapp(numero, "Por ahora solo puedo leer mensajes de texto 😊. Escríbeme tu pedido.")
            return {"status": "ok"}

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
        print(f"De {numero}: {texto}")

        if numero == ADMIN_NUMBER:
            respuesta_admin = procesar_comando_admin(texto)
            if respuesta_admin:
                enviar_whatsapp(numero, respuesta_admin)
                return {"status": "ok"}

        if not esta_abierto() and numero != ADMIN_NUMBER:
            enviar_whatsapp(numero,
                "¡Hola! 😊 Gracias por escribirnos. Por ahora estamos cerrados.\n"
                "Nuestro horario es de *1:00pm a 11:00pm*. ¡Te esperamos pronto! 🍔")
            return {"status": "ok"}

        # ─── ¿EL CLIENTE ESTÁ RESPONDIENDO A "MODIFICAR O PEDIDO NUEVO"? ──────
        texto_lower = texto.lower()
        saltar_palabras_clave = False

        if numero in clientes_esperando_decision:
            mensaje_original = clientes_esperando_decision.pop(numero)

            quiere_modificar = any(p in texto_lower for p in
                ["modificar", "el mismo", "modificarlo", "ese pedido", "el actual", "actualizar"])
            quiere_nuevo = any(p in texto_lower for p in
                ["nuevo", "otro pedido", "uno nuevo", "aparte", "diferente", "distinto"])

            if quiere_modificar:
                pedido_pendiente = buscar_pedido_cliente(numero)
                if pedido_pendiente:
                    pedido_pendiente["modificaciones"].append(mensaje_original)
                    enviar_whatsapp(numero, "✅ Listo, hemos anotado ese cambio en tu pedido.\n📝 ¡Nuestro equipo lo procesará! 🍔")
                    enviar_whatsapp(
                        ADMIN_NUMBER,
                        f"📝 *El pedido #{pedido_pendiente['id']} ha sido modificado*\n"
                        f"📱 Cliente: +{numero}\n"
                        f"────────────────\n"
                        f"{mensaje_original}\n"
                        f"────────────────\n"
                        f"👉 Revisa el panel para más detalles."
                    )
                else:
                    enviar_whatsapp(numero, "Tu pedido anterior ya no está activo. ¿Quieres hacer un pedido nuevo? Cuéntame qué deseas 😊")
                return {"status": "ok"}

            elif quiere_nuevo:
                # Arrancamos una conversación nueva, separada del pedido anterior,
                # usando el mensaje original que generó la duda.
                historial[numero] = []
                texto = mensaje_original
                texto_lower = texto.lower()
                saltar_palabras_clave = True  # ya sabemos que es un pedido nuevo, no una palabra clave

            else:
                # No quedó claro -> volvemos a preguntar
                clientes_esperando_decision[numero] = mensaje_original
                enviar_whatsapp(numero, "Disculpa, no quedó claro 😊 ¿Quieres *modificar* tu pedido actual o hacer un *pedido nuevo*? Responde 'modificar' o 'nuevo'.")
                return {"status": "ok"}

        # ─── OPCIONES ESPECIALES DEL CLIENTE ─────────────────────────────────
        if not saltar_palabras_clave:
            # ELIMINAR / CANCELAR PEDIDO
            if any(p in texto_lower for p in ["eliminar pedido", "cancelar pedido", "quiero eliminar", "cancela mi pedido"]):
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["estado"] = "cancelado"
                    enviar_whatsapp(numero, f"❌ Tu pedido #{pedido['id']} ha sido cancelado.\nSi cambias de idea, ¡escríbenos! 🍔")
                    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ *Pedido #{pedido['id']} cancelado*\nCliente +{numero} canceló su pedido.")
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo para cancelar. ¿Deseas hacer un nuevo pedido?")
                return {"status": "ok"}

            # MODIFICAR PEDIDO
            if any(p in texto_lower for p in ["modificar pedido", "cambiar plato", "quiero cambiar", "agregar algo"]):
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["modificaciones"].append(texto)
                    enviar_whatsapp(numero, "✅ Hemos recibido tu solicitud de cambio.\n📝 ¡Nuestro equipo lo procesará! 🍔")
                    # Notificación clara al admin de que el pedido fue modificado
                    enviar_whatsapp(
                        ADMIN_NUMBER,
                        f"📝 *El pedido #{pedido['id']} ha sido modificado*\n"
                        f"📱 Cliente: +{numero}\n"
                        f"────────────────\n"
                        f"{texto}\n"
                        f"────────────────\n"
                        f"👉 Revisa el panel para más detalles."
                    )
                else:
                    enviar_whatsapp(numero, "No encontramos un pedido activo. ¿Deseas hacer un nuevo pedido?")
                return {"status": "ok"}

            # QUEJAS / RECLAMACIONES
            if any(p in texto_lower for p in ["queja", "reclamación", "problema", "cambio de plato", "está mal", "no me gusta"]):
                pedido = buscar_pedido_cliente(numero)
                if pedido:
                    pedido["quejas"].append(texto)
                    enviar_whatsapp(numero, "⚠️ Hemos recibido tu reclamación.\n👨‍💼 Nuestro equipo se pondrá en contacto contigo pronto.\n¡Disculpas! 😟")
                    enviar_whatsapp(ADMIN_NUMBER, f"⚠️ *QUEJA - Pedido #{pedido['id']}*\n📱 +{numero}\n{texto}")
                else:
                    enviar_whatsapp(numero, "Cuéntanos qué pasó para que podamos ayudarte mejor 😊")
                return {"status": "ok"}

            # ─── ¿TIENE UN PEDIDO ACTIVO RECIENTE Y ESCRIBE ALGO AMBIGUO? ────
            # Si ya tiene un pedido activo creado hace poco y escribe de nuevo
            # sin usar ninguna palabra clave explícita, le preguntamos qué quiere.
            pedido_activo = buscar_pedido_cliente(numero)
            if pedido_activo and pedido_es_reciente(pedido_activo):
                clientes_esperando_decision[numero] = texto
                enviar_whatsapp(
                    numero,
                    f"👋 Veo que ya tienes un pedido activo (#{pedido_activo['id']}). "
                    f"¿Quieres *modificar* ese pedido o hacer un *pedido nuevo*? "
                    f"Responde 'modificar' o 'nuevo' 😊"
                )
                return {"status": "ok"}

        # ─── CONVERSACIÓN NORMAL CON CLAUDE ──────────────────────────────────
        if numero not in historial:
            historial[numero] = []
        historial[numero].append({"role": "user", "content": texto})

        ai = anthropic.Anthropic(api_key=CLAUDE_KEY)
        resp = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=build_system_prompt(),
            messages=historial[numero],
        )
        texto_respuesta = resp.content[0].text
        print(f"Respuesta: {texto_respuesta}")
        historial[numero].append({"role": "assistant", "content": texto_respuesta})

        if len(historial[numero]) > 30:
            historial[numero] = historial[numero][-30:]

        enviar_whatsapp(numero, texto_respuesta)

        # Detectar cierre de pedido
        palabras_cierre = ["en camino", "ya está en camino", "listo para recoger", "pasamos a preparar", "empezamos a preparar"]
        tiene_contexto = any(p in texto_respuesta.lower() for p in ["domicilio", "recoger", "local"])
        es_cierre = any(p in texto_respuesta.lower() for p in palabras_cierre)

        if es_cierre and tiene_contexto:
            resumen = texto_respuesta
            for msg in reversed(historial[numero]):
                if msg["role"] == "assistant":
                    c = msg["content"].lower()
                    if "total" in c and "$" in c:
                        resumen = msg["content"]
                        break

            pedido, es_nuevo = registrar_pedido(numero, resumen, texto_respuesta)
            if es_nuevo:
                notificar_pedido_admin(numero, pedido)
            else:
                notificar_pedido_actualizado_admin(numero, pedido)

    except Exception:
        traceback.print_exc()

    return {"status": "ok"}
