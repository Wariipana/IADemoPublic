"""
setup.py — Ejecutar UNA SOLA VEZ para:
  1. Autenticar con Gmail (abre el navegador)
  2. Registrar el watch de Gmail → Pub/Sub
  3. Verificar la conexión con Twilio
  4. Verificar la conexión con Groq

Uso:
  python setup.py --webhook-url https://tu-app.railway.app
"""

import asyncio
import argparse
import sys


def check_env():
    from config import settings
    required = [
        "groq_api_key", "twilio_account_sid", "twilio_auth_token",
        "twilio_whatsapp_from", "twilio_whatsapp_to",
        "gmail_credentials_json", "google_cloud_project",
    ]
    missing = [f for f in required if not getattr(settings, f, None)]
    if missing:
        print(f"❌ Variables de entorno faltantes: {', '.join(missing)}")
        sys.exit(1)
    print("✅ Variables de entorno OK")


async def test_groq():
    from services.groq_service import GroqService
    g = GroqService()
    result = await g.free_chat("di 'hola' en una sola palabra")
    print(f"✅ Groq OK — respuesta: {result}")


async def test_twilio():
    from services.twilio_service import TwilioService
    t = TwilioService()
    await t.send_message("🤖 Agente de correo iniciado correctamente.")
    print("✅ Twilio OK — mensaje de prueba enviado a WhatsApp")


def setup_gmail_watch(webhook_url: str):
    from services.gmail_service import GmailService
    g = GmailService()
    result = g.setup_push_notifications()
    print(f"✅ Gmail watch registrado — historyId: {result.get('historyId')}")


async def main(webhook_url: str):
    print("\n=== Setup del Agente Email-WhatsApp ===\n")
    check_env()
    await test_groq()
    await test_twilio()
    setup_gmail_watch(webhook_url)
    print("\n🚀 Todo listo. Arranca el servidor con: uvicorn main:app --host 0.0.0.0 --port 8000\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--webhook-url",
        required=True,
        help="URL pública de tu app en Railway/Render (ej: https://mi-agente.railway.app)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.webhook_url))
