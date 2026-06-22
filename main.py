from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import anthropic, requests, os, traceback
from dotenv import load_dotenv
load_dotenv()
app = FastAPI()
CLAUDE_KEY = os.getenv("CLAUDE_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
historial = {}
SYSTEM_PROMPT = """Eres el asistente virtual de Sabores de Nariño, un bar de comidas rápidas ubicado en Cra 7 #6-43, Ipiales.
HORARIO: 4:00pm – 11:00pm
DOMICILIO: Sí. Costo: $6.000. Mínimo: Sin mínimo. Horario de domicilios igual al de atención.
MÉTODOS DE PAGO: Nequi, Daviplata, transferencia bancaria, efectivo.
MENÚ:
Hamburguesas: Sencilla $16.000 / Doble Carne $24.000 / Especial $22.000 / Mixta $26.000 / Ranchera $28.000
Perros: Sencillo $10.000 / Especial $14.000 / Ranchero $17.000
Salchipapas: Sencilla $13.000 / Especial $18.000 / Mixta $22.000 / Trifásica $28.000
Mazorcadas: Sencilla $16.000 / Mixta $22.000 / Especial $28.000
Burritos y Sándwiches: Burrito Pollo $18.000 / Burrito Mixto $21.000 / Sándwich Pollo $15.000 / Sándwich Especial $19.000
Otros: Papas Pequeñas $7.000 / Papas Grandes $11.000 / Nuggets 8und $14.000 / Choripapa $18.000 / Patacón Mixto $22.000
Bebidas: Gaseosa 250ml $3.000 / 400ml $4.500 / 1.5L $8.000 / Agua $3.000 / Té Frío $4.000 / Jugo Agua $5.000 / Jugo Leche $7.000 / Limonada $5.000 / Malteada $9.000 / Café $3.500
Combos: Hamburguesa Sencilla+Papas+Gaseosa $24.000 / Hamburguesa Especial+Papas+Gaseosa $30.000 / Perro Especial+Papas+Gaseosa $22.000 / Salchipapa Especial+Gaseosa $21.000 / Burrito Mixto+Gaseosa $27.000
INSTRUCCIONES:
- Habla amigable y natural como empleado real.
- Al pedir, confirma cada ítem con precio y muestra el total.
- Pregunta dirección si es domicilio.
- Si quiere hablar con persona real, dile que lo comunicas con el equipo.
- No inventes productos ni precios.
- Si no sabes algo, sugiere llamar directamente.
- Responde siempre en español."""

def enviar_whatsapp(numero, mensaje):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": mensaje}
    }
    r = requests.post(url, headers=headers, json=data)
    print("RESPUESTA DE META AL ENVIAR:", r.status_code, r.text)
    return r

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("Token invalido", status_code=403)

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    data = await request.json()
    print("DATOS RECIBIDOS:", data)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            print("No hay 'messages' en este evento (puede ser un status update)")
            return {"status": "ok"}

        mensaje = entry["messages"][0]
        numero = mensaje["from"]
        texto = mensaje["text"]["body"]
        print(f"Mensaje de {numero}: {texto}")

        if numero not in historial:
            historial[numero] = []
        historial[numero].append({"role": "user", "content": texto})

        cliente = anthropic.Anthropic(api_key=CLAUDE_KEY)
        respuesta = cliente.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=historial[numero]
        )
        texto_respuesta = respuesta.content[0].text
        print(f"Respuesta generada: {texto_respuesta}")
        historial[numero].append({"role": "assistant", "content": texto_respuesta})

        if len(historial[numero]) > 20:
            historial[numero] = historial[numero][-20:]

        enviar_whatsapp(numero, texto_respuesta)
        print("Mensaje enviado a WhatsApp")

    except Exception as e:
        print("ERROR COMPLETO:")
        traceback.print_exc()

    return {"status": "ok"}