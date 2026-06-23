from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import anthropic, requests, os, traceback, uuid
from dotenv import load_dotenv
from datetime import datetime
import pytz

load_dotenv()

app = FastAPI()
CLAUDE_KEY = os.getenv("CLAUDE_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "sabores2024")  # contraseña del panel

ADMIN_NUMBER = "573167731698"
ZONA_HORARIA = pytz.timezone("America/Bogota")

historial = {}
mensajes_procesados = set()

# ── PEDIDOS ─────────────────────────────────────────────────────────────────
# Lista en memoria. Cada pedido es un dict:
# { id, numero, hora, resumen, direccion, tipo, estado, modificaciones, quejas }
# estado: "activo" | "preparando" | "enviado" | "entregado" | "cancelado"
pedidos = []


def registrar_pedido(numero_cliente, resumen, confirmacion_bot):
    """Crea un pedido nuevo en la lista al detectar cierre de conversación."""
    # Detectar si es domicilio o recoger
    es_domicilio = "camino" in confirmacion_bot.lower() or "domicilio" in confirmacion_bot.lower()
    tipo = "domicilio" if es_domicilio else "recoger"

    # Extraer dirección del mensaje de confirmación del bot
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
        "modificaciones": [],  # Cambios solicitados por cliente
        "quejas": [],          # Problemas reportados
        "cambios_platos": [],  # Cambios de platos
        "notas_admin": [],     # Notas internas del admin (NO se envían)
    }
    pedidos.append(pedido)
    # Mantener solo los últimos 100 pedidos en memoria
    if len(pedidos) > 100:
        pedidos.pop(0)
    return pedido


def buscar_pedido_cliente(numero_cliente):
    """Busca el último pedido activo de un cliente."""
    for p in reversed(pedidos):
        if p["numero"] == numero_cliente and p["estado"] in ["activo", "preparando"]:
            return p
    return None


# ── MENÚ ─────────────────────────────────────────────────────────────────────
menu = {
    "hamburguesas": "Hamburguesas: Sencilla $16.000 / Doble Carne $24.000 / Especial $22.000 / Mixta $26.000 / Ranchera $28.000",
    "perros": "Perros: Sencillo $10.000 / Especial $14.000 / Ranchero $17.000",
    "salchipapas": "Salchipapas: Sencilla $13.000 / Especial $18.000 / Mixta $22.000 / Trifásica $28.000",
    "mazorcadas": "Mazorcadas: Sencilla $16.000 / Mixta $22.000 / Especial $28.000",
    "burritos": "Burritos y Sándwiches: Burrito Pollo $18.000 / Burrito Mixto $21.000 / Sándwich Pollo $15.000 / Sándwich Especial $19.000",
    "otros": "Otros: Papas Pequeñas $7.000 / Papas Grandes $11.000 / Nuggets 8und $14.000 / Choripapa $18.000 / Patacón Mixto $22.000",
    "bebidas": "Bebidas: Gaseosa 250ml $3.000 / 400ml $4.500 / 1.5L $8.000 / Agua $3.000 / Té Frío $4.000 / Jugo Agua $5.000 / Jugo Leche $7.000 / Limonada $5.000 / Malteada $9.000 / Café $3.500",
    "combos": "Combos: Hamburguesa Sencilla+Papas+Gaseosa $24.000 / Hamburguesa Especial+Papas+Gaseosa $30.000 / Perro Especial+Papas+Gaseosa $22.000 / Salchipapa Especial+Gaseosa $21.000 / Burrito Mixto+Gaseosa $27.000",
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

    return f"""Eres el asistente virtual de Sabores de Nariño, comidas rápidas en Cra 7 #6-43, Ipiales.
HORARIO: 1:00pm – 11:00pm
DOMICILIO: {domicilio_txt}
MÉTODOS DE PAGO: Nequi, Daviplata, transferencia, efectivo.
MENÚ:
{chr(10).join(menu_activo)}
{notas}{espera_txt}

OPCIONES ADICIONALES PARA CLIENTES:
- Para ELIMINAR su pedido activo, el cliente debe escribir "eliminar pedido" o "cancelar pedido"
- Para MODIFICAR su pedido (cambiar un plato por otro), debe escribir "modificar pedido" o "cambiar plato"
- Para REPORTAR QUEJAS o cambios de platos, debe escribir "queja", "reclamación", "problema" o "cambio de plato"

INSTRUCCIONES CRÍTICAS PARA MANEJO DEL PEDIDO:
- Habla amigable y natural como empleado real.
- Acumula TODOS los productos que el cliente pide sin mostrar resumen parcial.
- NUNCA muestres resumen ni total hasta que el cliente diga "es todo", "eso sería", "listo", "ya es todo", "nada más" o similar.
- Solo entonces muestra el resumen completo con todos los productos y el total.
- Luego pregunta: ¿Es para domicilio o para recoger en el local?
- Si domicilio: pide la dirección. Al recibirla confirma con exactamente: "Perfecto, domicilio a [dirección]. Tu pedido ya está en camino 🛵"
- Si recoger: confirma con: "Perfecto, tu pedido estará listo para recoger en Cra 7 #6-43 🍔"
- No repitas el resumen ni el total después de pedir/confirmar la dirección.
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
    """Notifica al admin por WhatsApp con los detalles del pedido."""
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


# ── PANEL HTML ───────────────────────────────────────────────────────────────

PANEL_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel de Pedidos - Sabores de Nariño</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
            color: #fff;
            padding: 20px;
            min-height: 100vh;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        header {
            text-align: center;
            margin-bottom: 30px;
            border-bottom: 2px solid #f5a623;
            padding-bottom: 20px;
        }
        h1 {
            font-size: 2.5rem;
            color: #f5a623;
            margin-bottom: 5px;
        }
        .header-info {
            display: flex;
            justify-content: center;
            gap: 30px;
            flex-wrap: wrap;
            margin-top: 15px;
            font-size: 0.9rem;
            color: #ccc;
        }
        .pedidos-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .pedido-card {
            background: linear-gradient(135deg, #2d2d2d 0%, #3d3d3d 100%);
            border-left: 4px solid #f5a623;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.3);
            transition: transform 0.2s;
        }
        .pedido-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 6px 20px rgba(245, 166, 35, 0.2);
        }
        .pedido-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 15px;
            border-bottom: 1px solid #444;
            padding-bottom: 10px;
        }
        .pedido-id {
            font-size: 1.3rem;
            font-weight: bold;
            color: #f5a623;
        }
        .pedido-hora {
            font-size: 0.85rem;
            color: #aaa;
        }
        .pedido-tipo {
            display: inline-block;
            background: #f5a623;
            color: #1a1a1a;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: bold;
            margin-top: 5px;
        }
        .pedido-cliente {
            font-size: 0.9rem;
            color: #ccc;
            margin: 10px 0;
        }
        .pedido-direccion {
            font-size: 0.85rem;
            color: #aaa;
            margin: 8px 0;
            word-break: break-word;
        }
        .pedido-resumen {
            background: #1a1a1a;
            padding: 10px;
            border-radius: 4px;
            font-size: 0.85rem;
            color: #ddd;
            margin: 12px 0;
            max-height: 150px;
            overflow-y: auto;
            border-left: 3px solid #f5a623;
        }
        .estado-select {
            width: 100%;
            padding: 8px;
            background: #1a1a1a;
            border: 1px solid #f5a623;
            border-radius: 4px;
            color: #fff;
            margin: 12px 0;
            cursor: pointer;
        }
        .estado-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: bold;
            margin-right: 5px;
        }
        .estado-activo { background: #4CAF50; color: #fff; }
        .estado-preparando { background: #FF9800; color: #fff; }
        .estado-enviado { background: #2196F3; color: #fff; }
        .estado-entregado { background: #9C27B0; color: #fff; }
        .estado-cancelado { background: #f44336; color: #fff; }
        
        .modificaciones {
            background: #1a1a1a;
            padding: 10px;
            border-radius: 4px;
            margin: 10px 0;
            font-size: 0.85rem;
            border-left: 3px solid #FF9800;
        }
        .quejas {
            background: #1a1a1a;
            padding: 10px;
            border-radius: 4px;
            margin: 10px 0;
            font-size: 0.85rem;
            border-left: 3px solid #f44336;
        }
        .cambios-platos {
            background: #1a1a1a;
            padding: 10px;
            border-radius: 4px;
            margin: 10px 0;
            font-size: 0.85rem;
            border-left: 3px solid #00BCD4;
        }
        
        button {
            background: #f5a623;
            border: none;
            color: #1a1a1a;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: bold;
            font-size: 0.9rem;
            transition: background 0.2s;
            width: 100%;
            margin-top: 10px;
        }
        button:hover { background: #e09510; }
        
        .logout-btn {
            background: #f44336;
            color: white;
            padding: 10px 20px;
            border-radius: 4px;
            border: none;
            cursor: pointer;
            float: right;
            margin-top: 0;
            width: auto;
        }
        .logout-btn:hover { background: #d32f2f; }
        
        .login-box {
            background: linear-gradient(135deg, #2d2d2d 0%, #3d3d3d 100%);
            border: 2px solid #f5a623;
            border-radius: 10px;
            padding: 40px;
            max-width: 400px;
            margin: 100px auto;
            text-align: center;
        }
        .login-box h1 { font-size: 2rem; margin-bottom: 30px; }
        .login-box input {
            width: 100%;
            padding: 12px;
            margin-bottom: 12px;
            background: #1a1a1a;
            border: 1px solid #f5a623;
            border-radius: 10px;
            color: #fff;
            font-size: 1rem;
            outline: none;
        }
        .login-box input:focus { border-color: #e09510; }
        .login-box button {
            width: 100%;
            padding: 12px;
            background: #f5a623;
            border: none;
            border-radius: 10px;
            color: #1a1a1a;
            font-weight: 700;
            font-size: 1rem;
            cursor: pointer;
        }
        .login-box button:hover { background: #e09510; }
        
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #aaa;
        }
        
        .notas-admin {
            background: #1a1a1a;
            padding: 10px;
            border-radius: 4px;
            margin: 10px 0;
            font-size: 0.85rem;
            border-left: 3px solid #9C27B0;
        }
        
        /* MODAL STYLES */
        #modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        
        .modal-box {
            background: linear-gradient(135deg, #2d2d2d 0%, #3d3d3d 100%);
            border: 2px solid #f5a623;
            border-radius: 10px;
            padding: 30px;
            max-width: 500px;
            width: 90%;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
        }
        
        .modal-box h2 {
            color: #f5a623;
            margin-bottom: 20px;
            font-size: 1.3rem;
        }
        
        .modal-box textarea {
            width: 100%;
            padding: 12px;
            background: #1a1a1a;
            border: 1px solid #f5a623;
            border-radius: 6px;
            color: #fff;
            font-family: inherit;
            font-size: 0.95rem;
            resize: vertical;
            min-height: 100px;
            margin-bottom: 15px;
            box-sizing: border-box;
        }
        
        .modal-box textarea:focus {
            outline: none;
            border-color: #e09510;
            box-shadow: 0 0 8px rgba(245, 166, 35, 0.3);
        }
        
        .modal-buttons {
            display: flex;
            gap: 10px;
        }
        
        .modal-buttons button {
            flex: 1;
            padding: 12px;
            border: none;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            font-size: 0.95rem;
            margin-top: 0;
            width: 100%;
        }
        
        .modal-guardar {
            background: #4CAF50;
            color: white;
        }
        
        .modal-guardar:hover {
            background: #45a049;
        }
        
        .modal-cancelar {
            background: #666;
            color: white;
        }
        
        .modal-cancelar:hover {
            background: #555;
        }
        
        #modal-exito-msg {
            display: none;
            position: fixed;
            top: 20px;
            right: 20px;
            background: #4CAF50;
            color: white;
            padding: 15px 25px;
            border-radius: 6px;
            font-weight: bold;
            z-index: 1001;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }
    </style>
</head>
<body>
<div class="container">
    <header id="header" style="display: none;">
        <h1>🍔 Sabores de Nariño - Panel de Pedidos</h1>
        <div style="position: absolute; right: 20px; top: 20px;">
            <button onclick="cargarPedidos()" style="background: #4CAF50; color: white; padding: 10px 20px; margin-right: 10px; border: none; border-radius: 6px; cursor: pointer; font-weight: bold;">🔄 Actualizar</button>
            <button class="logout-btn" onclick="logout()">Cerrar sesión</button>
        </div>
        <div class="header-info" style="margin-top: 15px;">
            <span id="tiempo-actual">🕐 --:--</span>
            <span id="total-pedidos">📊 Total: 0</span>
            <span id="pedidos-activos">⚡ Activos: 0</span>
        </div>
    </header>
    
    <div id="login-section" class="login-box">
        <h1>🍔 Sabores de Nariño</h1>
        <p style="margin-bottom: 20px; color: #aaa;">Panel de pedidos</p>
        <input type="password" id="pw" placeholder="Contraseña" autocomplete="current-password" onkeypress="if(event.key==='Enter'){event.preventDefault(); login();}" onkeydown="if(event.keyCode===13){event.preventDefault(); login();}"
        <button onclick="login()">Entrar</button>
    </div>
    
    <div id="pedidos-section" style="display: none;">
        <div id="pedidos-container" class="pedidos-grid"></div>
        <div id="empty-state" class="empty-state" style="display: none;">No hay pedidos aún</div>
    </div>
    
    <!-- MODAL PARA EDITAR PEDIDOS -->
    <div id="modal-overlay">
        <div class="modal-box">
            <h2 id="modal-titulo">Actualizar pedido</h2>
            <textarea id="modal-texto" placeholder="Escribe aquí..."></textarea>
            <div class="modal-buttons">
                <button class="modal-guardar" onclick="guardarModificacion()">✓ Guardar</button>
                <button class="modal-cancelar" onclick="cerrarModal()">✕ Cancelar</button>
            </div>
        </div>
    </div>
    
    <div id="modal-exito-msg">✅ ¡Guardado exitosamente!</div>
</div>

<script>
let password = '';

function login() {
    const pw = document.getElementById('pw').value;
    
    if (!pw) {
        alert('Por favor ingresa la contraseña');
        return;
    }
    
    console.log('[DEBUG] Intentando login...');
    password = pw;
    
    // Mostrar UI de carga
    document.getElementById('login-section').style.display = 'none';
    document.getElementById('header').style.display = 'block';
    document.getElementById('pedidos-section').style.display = 'block';
    
    // Iniciar recarga automática y cargar pedidos
    iniciarRecargaAutomatica();
}

function logout() {
    console.log('[DEBUG] Cerrando sesión');
    detenerRecargaAutomatica();
    password = '';
    document.getElementById('login-section').style.display = 'block';
    document.getElementById('header').style.display = 'none';
    document.getElementById('pedidos-section').style.display = 'none';
    document.getElementById('pw').value = '';
    document.getElementById('pw').focus();
}

async function cargarPedidos() {
    try {
        console.log('[DEBUG] Cargando pedidos con password:', password ? '****' : 'vacío');
        
        if (!password) {
            console.error('[ERROR] No hay contraseña');
            alert('Contraseña vacía. Por favor inicia sesión.');
            return;
        }
        
        const url = `/api/pedidos?pw=${encodeURIComponent(password)}`;
        console.log('[DEBUG] URL:', url);
        
        const response = await fetch(url);
        console.log('[DEBUG] Response status:', response.status);
        
        if (!response.ok) {
            console.error('[ERROR] Response no OK:', response.status, response.statusText);
            if (response.status === 403) {
                alert('❌ Contraseña incorrecta. Sesión cerrada.');
                logout();
            } else {
                alert('❌ Error al cargar pedidos: ' + response.statusText);
            }
            return;
        }
        
        const data = await response.json();
        console.log('[DEBUG] Pedidos recibidos:', data.pedidos.length);
        
        renderizarPedidos(data.pedidos);
        actualizarStats(data.pedidos);
        console.log('[SUCCESS] Pedidos cargados correctamente');
    } catch (error) {
        console.error('[ERROR] Excepción en cargarPedidos:', error);
        alert('❌ Error al cargar pedidos:\n' + error.message);
    }
}

function renderizarPedidos(pedidos) {
    const container = document.getElementById('pedidos-container');
    const emptyState = document.getElementById('empty-state');
    
    if (pedidos.length === 0) {
        container.innerHTML = '';
        emptyState.style.display = 'block';
        return;
    }
    emptyState.style.display = 'none';
    
    container.innerHTML = pedidos.map(p => `
        <div class="pedido-card">
            <div class="pedido-header">
                <div>
                    <div class="pedido-id">#${p.id}</div>
                    <div class="pedido-hora">🕐 ${p.hora}</div>
                </div>
            </div>
            
            <div class="pedido-tipo">${p.tipo === 'domicilio' ? '🛵 Domicilio' : '🏠 Recoger'}</div>
            
            <div class="pedido-cliente">📱 ${p.numero}</div>
            <div class="pedido-direccion">📍 ${p.direccion}</div>
            
            <div class="pedido-resumen">${p.resumen}</div>
            
            ${p.modificaciones && p.modificaciones.length > 0 ? `
                <div class="modificaciones">
                    <strong>📝 Modificaciones del cliente:</strong><br>
                    ${p.modificaciones.map(m => `<div style="margin: 5px 0; padding: 5px; background: #0a0a0a; border-radius: 3px;">${m}</div>`).join('')}
                </div>
            ` : ''}
            
            ${p.quejas && p.quejas.length > 0 ? `
                <div class="quejas">
                    <strong>⚠️ Quejas/Reclamaciones:</strong><br>
                    ${p.quejas.map(q => `<div style="margin: 5px 0; padding: 5px; background: #0a0a0a; border-radius: 3px;">${q}</div>`).join('')}
                </div>
            ` : ''}
            
            ${p.cambios_platos && p.cambios_platos.length > 0 ? `
                <div class="cambios-platos">
                    <strong>🔄 Cambios de Platos:</strong><br>
                    ${p.cambios_platos.map(c => `<div style="margin: 5px 0; padding: 5px; background: #0a0a0a; border-radius: 3px;">${c}</div>`).join('')}
                </div>
            ` : ''}
            
            ${p.notas_admin && p.notas_admin.length > 0 ? `
                <div class="notas-admin">
                    <strong>📌 Notas Internas (Admin):</strong><br>
                    ${p.notas_admin.map(n => `<div style="margin: 5px 0; padding: 5px; background: #0a0a0a; border-radius: 3px; font-size: 0.8rem; color: #aaa;">${n}</div>`).join('')}
                </div>
            ` : ''}
            
            <div style="margin-top: 15px; border-top: 1px solid #444; padding-top: 10px;">
                <label style="display: block; margin-bottom: 8px; font-weight: bold;">Estado:</label>
                <select class="estado-select" onchange="cambiarEstado('${p.id}', this.value)">
                    <option value="activo" ${p.estado === 'activo' ? 'selected' : ''}>Activo</option>
                    <option value="preparando" ${p.estado === 'preparando' ? 'selected' : ''}>Preparando</option>
                    <option value="enviado" ${p.estado === 'enviado' ? 'selected' : ''}>Enviado</option>
                    <option value="entregado" ${p.estado === 'entregado' ? 'selected' : ''}>Entregado</option>
                    <option value="cancelado" ${p.estado === 'cancelado' ? 'selected' : ''}>Cancelado</option>
                </select>
            </div>
            
            <div style="text-align: center; margin: 10px 0;">
                <span class="estado-badge estado-${p.estado}">${p.estado.toUpperCase()}</span>
            </div>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px;">
                <button onclick="abrirModal('${p.id}', 'modificacion')" style="background: #FF9800; margin-top: 0;">📝 Actualizar</button>
                <button onclick="abrirModal('${p.id}', 'cambio')" style="background: #00BCD4; margin-top: 0;">🔄 Cambio</button>
                <button onclick="abrirModal('${p.id}', 'nota')" style="background: #9C27B0; margin-top: 0;">📌 Nota</button>
                <button onclick="abrirModal('${p.id}', 'queja')" style="background: #f44336; margin-top: 0;">⚠️ Queja</button>
            </div>
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
    const ahora = new Date();
    const horas = String(ahora.getHours()).padStart(2, '0');
    const minutos = String(ahora.getMinutes()).padStart(2, '0');
    
    const tiempoActual = `🕐 ${horas}:${minutos}`;
    const totalPedidos = `📊 Total: ${pedidos.length}`;
    const activos = pedidos.filter(p => p.estado === 'activo' || p.estado === 'preparando').length;
    const pedidosActivos = `⚡ Activos: ${activos}`;
    
    const tiempoEl = document.getElementById('tiempo-actual');
    const totalEl = document.getElementById('total-pedidos');
    const activosEl = document.getElementById('pedidos-activos');
    
    if (tiempoEl) tiempoEl.textContent = tiempoActual;
    if (totalEl) totalEl.textContent = totalPedidos;
    if (activosEl) activosEl.textContent = pedidosActivos;
    
    console.log('[DEBUG]', tiempoActual, totalPedidos, pedidosActivos);
}

// Recargar cada 5 segundos SOLO si estamos en la sección de pedidos
let intervaloRecarga = null;

function iniciarRecargaAutomatica() {
    if (intervaloRecarga) clearInterval(intervaloRecarga);
    console.log('[DEBUG] Iniciando recarga automática cada 5 segundos');
    cargarPedidos(); // Primera carga inmediata
    intervaloRecarga = setInterval(() => {
        if (document.getElementById('pedidos-section').style.display !== 'none') {
            console.log('[DEBUG] Recargando pedidos automáticamente');
            cargarPedidos();
        }
    }, 5000);
}

function detenerRecargaAutomatica() {
    if (intervaloRecarga) {
        clearInterval(intervaloRecarga);
        console.log('[DEBUG] Deteniendo recarga automática');
    }
}

// MODAL PARA AGREGAR MODIFICACIONES
let modalActual = { pedidoId: null, tipo: null };

function abrirModal(pedidoId, tipo) {
    modalActual.pedidoId = pedidoId;
    modalActual.tipo = tipo;
    
    let titulo = '';
    let placeholder = '';
    switch(tipo) {
        case 'modificacion':
            titulo = 'Actualizar pedido';
            placeholder = 'Ej: Agrega extra queso, cambia la salsa, etc...';
            break;
        case 'cambio':
            titulo = 'Registrar cambio de plato';
            placeholder = 'Ej: Cliente quiere otro plato en lugar de...';
            break;
        case 'nota':
            titulo = 'Agregar nota interna (solo para ti)';
            placeholder = 'Ej: Cliente llamó diciendo que llegue rápido...';
            break;
        case 'queja':
            titulo = 'Registrar reclamación del cliente';
            placeholder = 'Ej: Producto llegó frio, faltó ingrediente, etc...';
            break;
    }
    
    document.getElementById('modal-titulo').textContent = titulo;
    document.getElementById('modal-texto').placeholder = placeholder;
    document.getElementById('modal-texto').value = '';
    document.getElementById('modal-overlay').style.display = 'flex';
    document.getElementById('modal-texto').focus();
}

function cerrarModal() {
    document.getElementById('modal-overlay').style.display = 'none';
    modalActual = { pedidoId: null, tipo: null };
}

async function guardarModificacion() {
    const texto = document.getElementById('modal-texto').value.trim();
    if (!texto) {
        alert('Escribe algo antes de guardar');
        return;
    }
    
    if (!modalActual.pedidoId || !modalActual.tipo) {
        alert('Error: faltan datos del pedido');
        return;
    }
    
    console.log('[DEBUG] Guardando modificación:', {
        pedidoId: modalActual.pedidoId,
        tipo: modalActual.tipo,
        texto: texto.substring(0, 50) + '...'
    });
    
    try {
        const response = await fetch(`/api/pedidos/${modalActual.pedidoId}/modificacion`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                pw: password, 
                modificacion: texto,
                tipo: modalActual.tipo
            })
        });
        
        console.log('[DEBUG] Response status:', response.status);
        
        if (!response.ok) {
            const error = await response.text();
            throw new Error(`HTTP ${response.status}: ${error}`);
        }
        
        const result = await response.json();
        console.log('[SUCCESS] Modificación guardada:', result);
        
        // Mostrar mensaje de éxito
        const msgExito = document.getElementById('modal-exito-msg');
        msgExito.style.display = 'block';
        setTimeout(() => {
            msgExito.style.display = 'none';
        }, 2000);
        
        // Limpiar input
        document.getElementById('modal-texto').value = '';
        
        // Cerrar modal después de un segundo
        setTimeout(() => {
            cerrarModal();
            // Recargar pedidos inmediatamente
            console.log('[DEBUG] Recargando pedidos después de guardar');
            cargarPedidos();
        }, 500);
        
    } catch (error) {
        console.error('[ERROR] Error al guardar:', error);
        alert('❌ Error al guardar:\n' + error.message);
    }
}

// Cerrar modal con tecla ESC
document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
        cerrarModal();
    }
});

// Enfocar password al cargar
document.addEventListener('DOMContentLoaded', function() {
    console.log('[DEBUG] Página cargada');
    const pwInput = document.getElementById('pw');
    if (pwInput) {
        pwInput.focus();
        console.log('[DEBUG] Password input enfocado');
    }
});
</script>
</body>
</html>
"""


@app.get("/")
async def raiz():
    return HTMLResponse(
        """<html><body><h1>Bot de Sabores de Nariño</h1>
        <p>Sistema operativo. Accede a /panel para ver pedidos.</p></body></html>""")


@app.get("/panel")
async def panel(pw: str = ""):
    if pw and pw == PANEL_PASSWORD:
        html = PANEL_HTML.replace("{{PANEL_PASSWORD}}", PANEL_PASSWORD)
        return HTMLResponse(html)

    return HTMLResponse("""<html><head><meta charset="utf-8"><style>
  body{background:#1a1a1a;font-family:'Segoe UI';display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .box{background:#2d2d2d;border:2px solid #f5a623;padding:30px;border-radius:10px;text-align:center;color:#fff}
  h1{color:#f5a623;margin-bottom:5px}
  p{color:#aaa;margin-bottom:20px}
  input{width:100%;padding:12px;background:#1a1a1a;border:1px solid #f5a623;border-radius:10px;color:#fff;font-size:1rem;outline:none;margin-bottom:12px}
  input:focus{border-color:#f5a623}
  button{width:100%;padding:12px;background:#f5a623;border:none;border-radius:10px;
         color:#1a1a1a;font-weight:700;font-size:1rem;cursor:pointer}
  button:hover{background:#e09510}
</style></head><body>
<div class="box">
  <h1>🍔 Sabores de Nariño</h1>
  <p>Panel de pedidos</p>
  <input type="password" id="pw" placeholder="Contraseña" onkeydown="if(event.key==='Enter')login()">
  <button onclick="login()">Entrar</button>
</div>
<script>
function login(){
  const pw=document.getElementById('pw').value;
  window.location.href='/panel?pw='+encodeURIComponent(pw);
}
</script></body></html>
        """, status_code=200)

    # Panel principal — incrustado directamente, sin leer archivo externo
    html = PANEL_HTML.replace("{{PANEL_PASSWORD}}", PANEL_PASSWORD)
    return HTMLResponse(html)


@app.get("/api/pedidos")
async def api_pedidos(pw: str = ""):
    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403, detail="No autorizado")
    return {"pedidos": list(reversed(pedidos))}  # más recientes primero


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

    # Notificar al cliente cuando pasa a "enviado"
    if nuevo_estado == "enviado" and estado_anterior != "enviado":
        if pedido["tipo"] == "domicilio":
            msg = (
                f"🛵 *¡Tu pedido va en camino!*\n"
                f"Pedido #{pedido['id']} ha salido hacia {pedido['direccion']}.\n"
                f"¡Gracias por pedir en Sabores de Nariño! 🍔"
            )
        else:
            msg = (
                f"✅ *¡Tu pedido está listo!*\n"
                f"Pedido #{pedido['id']} está listo para recoger en Cra 7 #6-43.\n"
                f"¡Te esperamos! 🍔"
            )
        enviar_whatsapp(pedido["numero"], msg)

    # Notificar al cliente cuando pasa a "entregado"
    if nuevo_estado == "entregado" and estado_anterior != "entregado":
        enviar_whatsapp(
            pedido["numero"],
            f"🙌 *¡Pedido entregado!* Esperamos que lo disfrutes.\n"
            f"¡Gracias por elegirnos! Vuelve pronto 😊"
        )
    
    # Notificar al cliente cuando pasa a "cancelado"
    if nuevo_estado == "cancelado" and estado_anterior != "cancelado":
        enviar_whatsapp(
            pedido["numero"],
            f"❌ *Pedido #{pedido['id']} cancelado.*\n"
            f"Si tienes dudas, contáctanos.\n"
            f"¡Esperamos verte pronto! 🍔"
        )

    return {"ok": True, "pedido": pedido}


@app.post("/api/pedidos/{pedido_id}/modificacion")
async def agregar_modificacion(pedido_id: str, request: Request):
    body = await request.json()
    pw = body.get("pw", "")
    modificacion = body.get("modificacion", "")
    tipo = body.get("tipo", "modificacion")  # modificacion, nota, cambio

    if pw != PANEL_PASSWORD:
        raise HTTPException(status_code=403, detail="No autorizado")

    pedido = next((p for p in pedidos if p["id"] == pedido_id), None)
    if not pedido:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    if tipo == "modificacion":
        if "modificaciones" not in pedido:
            pedido["modificaciones"] = []
        pedido["modificaciones"].append(modificacion)
        mensaje_cliente = f"📝 *Actualización en tu pedido #{pedido_id}:*\n{modificacion}"
    elif tipo == "cambio":
        if "cambios_platos" not in pedido:
            pedido["cambios_platos"] = []
        pedido["cambios_platos"].append(modificacion)
        mensaje_cliente = f"🔄 *Cambio de plato en tu pedido #{pedido_id}:*\n{modificacion}"
    elif tipo == "nota":
        if "notas_admin" not in pedido:
            pedido["notas_admin"] = []
        pedido["notas_admin"].append(modificacion)
        # Las notas del admin NO se envían al cliente
        return {"ok": True, "pedido": pedido}

    # Notificar al cliente (excepto notas)
    enviar_whatsapp(pedido["numero"], mensaje_cliente)

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

        # Ignorar mensajes no-texto
        if mensaje.get("type") == "location":
            numero = mensaje["from"]
            loc = mensaje["location"]
            lat, lng = loc["latitude"], loc["longitude"]
            maps_link = f"https://maps.google.com/?q={lat},{lng}"
            # Buscar pedido activo de este cliente y actualizar dirección
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
        print(f"De {numero}: {texto}")

        # Admin
        if numero == ADMIN_NUMBER:
            respuesta_admin = procesar_comando_admin(texto)
            if respuesta_admin:
                enviar_whatsapp(numero, respuesta_admin)
                return {"status": "ok"}

        # Horario
        if not esta_abierto() and numero != ADMIN_NUMBER:
            enviar_whatsapp(numero,
                "¡Hola! 😊 Gracias por escribirnos. Por ahora estamos cerrados.\n"
                "Nuestro horario es de *1:00pm a 11:00pm*. ¡Te esperamos pronto! 🍔")
            return {"status": "ok"}

        # ─── OPCIONES ESPECIALES DE CLIENTE ─────────────────────────────────
        texto_lower = texto.lower()
        
        # Eliminar/Cancelar pedido
        if any(word in texto_lower for word in ["eliminar pedido", "cancelar pedido", "quiero eliminar", "cancela mi pedido"]):
            pedido = buscar_pedido_cliente(numero)
            if pedido:
                pedido["estado"] = "cancelado"
                enviar_whatsapp(numero, f"❌ Tu pedido #{pedido['id']} ha sido cancelado.\nSi cambias de idea, ¡escríbenos! 🍔")
                enviar_whatsapp(ADMIN_NUMBER, f"⚠️ Pedido #{pedido['id']} cancelado por cliente +{numero}")
            else:
                enviar_whatsapp(numero, "No encontramos un pedido activo para cancelar. ¿Deseas hacer un nuevo pedido?")
            return {"status": "ok"}
        
        # Modificar pedido
        if any(word in texto_lower for word in ["modificar pedido", "cambiar plato", "quiero cambiar", "agregar algo"]):
            pedido = buscar_pedido_cliente(numero)
            if pedido:
                if "modificaciones" not in pedido:
                    pedido["modificaciones"] = []
                pedido["modificaciones"].append(texto)
                enviar_whatsapp(numero, f"✅ Hemos recibido tu solicitud de cambio.\n📝 Tu mensaje fue registrado.\n¡Nuestro equipo lo procesará! 🍔")
                enviar_whatsapp(ADMIN_NUMBER, f"📝 MODIFICACIÓN Pedido #{pedido['id']}:\n+{numero}\n{texto}")
            else:
                enviar_whatsapp(numero, "No encontramos un pedido activo. ¿Deseas hacer un nuevo pedido?")
            return {"status": "ok"}
        
        # Quejas o cambios de platos
        if any(word in texto_lower for word in ["queja", "reclamación", "problema", "cambio de plato", "está mal", "no me gusta"]):
            pedido = buscar_pedido_cliente(numero)
            if pedido:
                if "quejas" not in pedido:
                    pedido["quejas"] = []
                pedido["quejas"].append(texto)
                enviar_whatsapp(numero, f"⚠️ Hemos recibido tu reclamación.\n👨‍💼 Nuestro equipo se pondrá en contacto contigo pronto.\n¡Disculpas! 😟")
                enviar_whatsapp(ADMIN_NUMBER, f"⚠️ QUEJA/RECLAMO Pedido #{pedido['id']}:\n+{numero}\n{texto}")
            else:
                enviar_whatsapp(numero, "Cuéntanos qué pasó para que podamos ayudarte mejor 😊")
            return {"status": "ok"}

        # ─── CONVERSACIÓN NORMAL DE PEDIDO ──────────────────────────────────
        # Claude
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
            # Buscar resumen con total en el historial
            resumen = texto_respuesta
            for msg in reversed(historial[numero]):
                if msg["role"] == "assistant":
                    c = msg["content"].lower()
                    if "total" in c and "$" in c:
                        resumen = msg["content"]
                        break

            pedido = registrar_pedido(numero, resumen, texto_respuesta)
            notificar_pedido_admin(numero, pedido)

    except Exception:
        traceback.print_exc()

    return {"status": "ok"}
