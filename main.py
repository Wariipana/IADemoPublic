from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import Response
import uvicorn

from services.gmail_service import GmailService
from services.groq_service import GroqService
from services.whatsapp_service import WhatsAppService
from services.session_service import SessionService
from config import settings

app = FastAPI(title="Email-WhatsApp Agent")

gmail   = GmailService()
groq    = GroqService()
twilio  = WhatsAppService()
session = SessionService()   # solo guarda el último correo para el reply


@app.on_event("startup")
async def startup():
    """Al arrancar, obtiene el historyId actual de Gmail para no perder
    el punto de referencia cuando el contenedor se reinicia."""
    try:
        service = gmail._get_service()
        profile = service.users().getProfile(userId=settings.gmail_user_id).execute()
        gmail._last_history_id = str(profile["historyId"])
        print(f"[Startup] historyId inicial de Gmail: {gmail._last_history_id}")
    except Exception as e:
        print(f"[Startup] No se pudo obtener historyId inicial: {e}")


# ---------------------------------------------------------------------------
# Gmail Push Notification (Pub/Sub)
# Única responsabilidad: avisar al usuario cuando llega un correo nuevo.
# No almacena nada — la fuente de verdad siempre es la API de Gmail.
# ---------------------------------------------------------------------------
@app.post("/webhook/gmail")
async def gmail_webhook(request: Request, background: BackgroundTasks):
    data = await request.json()
    background.add_task(handle_new_email, data)
    return {"status": "ok"}


async def handle_new_email(pubsub_data: dict):
    print(f"[Gmail Push] Notificación recibida: {pubsub_data}")
    email = await gmail.fetch_from_pubsub(pubsub_data)
    if not email:
        print("[Gmail Push] fetch_from_pubsub no devolvió correo — posiblemente no hay mensajes nuevos en el historial")
        return

    print(f"[Gmail Push] Correo obtenido: {email['subject']} de {email['from']}")
    classification = await groq.classify_email(email)
    print(f"[Gmail Push] Clasificación: {classification}")

    session.set_last_email(email)

    await twilio.send_message(build_notification(email, classification))
    print("[Gmail Push] Mensaje de WhatsApp enviado")


def build_notification(email: dict, classification: dict) -> str:
    emoji = {"alta": "🔴", "media": "🟡", "baja": "🟢"}.get(
        classification["relevancia"], "⚪"
    )
    return (
        f"{emoji} *Correo nuevo*\n"
        f"*De:* {email['from']}\n"
        f"*Asunto:* {email['subject']}\n"
        f"*Relevancia:* {classification['relevancia']}\n"
        f"_{classification['resumen_corto']}_\n\n"
        f"Responde con *reply:<mensaje>* para contestar este correo."
    )


# ---------------------------------------------------------------------------
# WhatsApp Webhook (Twilio)
# Resumen, conversaciones y búsquedas consultan Gmail API directamente.
# ---------------------------------------------------------------------------
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, background: BackgroundTasks):
    # Verificar el secreto compartido con el bridge
    secret = request.headers.get("x-api-secret", "")
    if secret != settings.wa_bridge_secret:
        return Response(status_code=401)

    # El bridge envía JSON, Twilio enviaba form data
    try:
        data = await request.json()
    except Exception:
        form = await request.form()
        data = {"Body": form.get("Body", ""), "From": form.get("From", "")}

    body   = (data.get("Body") or "").strip()
    sender = data.get("From", "")
    background.add_task(handle_whatsapp_message, body, sender)
    return Response(status_code=204)


async def handle_whatsapp_message(body: str, sender: str):
    lower = body.lower()

    if lower.startswith("resumen"):
        parts = body.split(maxsplit=1)
        periodo = parts[1].strip() if len(parts) > 1 else "hoy"
        await handle_summary(periodo)

    elif lower.startswith("correos"):
        parts = body.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else "hoy"
        await handle_list_emails(arg)

    elif lower.startswith("hilo"):
        parts = body.split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""
        await handle_thread(query)

    elif lower.startswith("reply:"):
        await handle_reply(body[6:].strip())

    elif lower.startswith("ayuda") or lower.startswith("help"):
        await handle_help()

    else:
        await handle_free_chat(body)


# ---------------------------------------------------------------------------
# Handlers — todos consultan Gmail API en tiempo real
# ---------------------------------------------------------------------------

async def handle_summary(periodo: str):
    await twilio.send_message("⏳ Consultando Gmail y generando resumen...")

    query = build_date_query(periodo)
    limit = 30 if "semana" in periodo else 20

    emails = await gmail.fetch_emails(query=query, max_results=limit)

    if not emails:
        await twilio.send_message(f"📭 No hay correos en tu bandeja para '{periodo}'.")
        return

    summary = await groq.summarize_emails(emails, periodo)
    await twilio.send_message(f"📋 *Resumen — {periodo}*\n\n{summary}")


async def handle_list_emails(arg: str):
    await twilio.send_message("⏳ Consultando Gmail...")

    if arg.isdigit():
        emails = await gmail.fetch_emails(max_results=int(arg))
    else:
        query = build_date_query(arg)
        emails = await gmail.fetch_emails(query=query, max_results=20)

    if not emails:
        await twilio.send_message("📭 No se encontraron correos.")
        return

    lines = []
    for e in emails:
        classification = await groq.classify_email(e)
        emoji = {"alta": "🔴", "media": "🟡", "baja": "🟢"}.get(
            classification["relevancia"], "⚪"
        )
        lines.append(f"{emoji} *{e['subject']}*\n  De: {e['from']}")

    await twilio.send_message(
        f"📬 *{len(emails)} correos encontrados*\n\n" + "\n\n".join(lines)
    )


async def handle_thread(query: str):
    if not query:
        await twilio.send_message("Indica el asunto o remitente. Ejemplo: *hilo proyecto final*")
        return

    await twilio.send_message(f"🔍 Buscando conversación sobre '{query}'...")

    emails = await gmail.fetch_emails(query=query, max_results=10)

    if not emails:
        await twilio.send_message(f"📭 No encontré conversaciones relacionadas con '{query}'.")
        return

    summary = await groq.summarize_thread(emails, query)
    await twilio.send_message(f"🧵 *Conversación: {query}*\n\n{summary}")


async def handle_reply(draft: str):
    last_email = session.get_last_email()

    if not last_email:
        await twilio.send_message(
            "⚠️ No hay ningún correo reciente para responder.\n"
            "Solo puedo responder correos que hayan llegado durante esta sesión."
        )
        return

    polished = await groq.polish_reply(draft, original_email=last_email)
    await gmail.send_reply(last_email, polished)
    await twilio.send_message(f"✅ Respuesta enviada a {last_email['from']}.")


async def handle_free_chat(message: str):
    recent = await gmail.fetch_emails(query="in:inbox", max_results=8)
    context = "\n\n".join(
        f"• Asunto: {e['subject']}\n  De: {e['from'].split('<')[0].strip()}\n  {e['body'][:800] or e['snippet']}"
        for e in recent
    )
    response = await groq.free_chat(message, context=context)
    await twilio.send_message(response)


async def handle_help():
    help_text = (
        "🤖 *Comandos disponibles*\n\n"
        "📋 *Resúmenes* (consulta Gmail en tiempo real)\n"
        "• *resumen* — correos de hoy\n"
        "• *resumen semana* — últimos 7 días\n"
        "• *resumen mes* — últimos 30 días\n\n"
        "📬 *Listar correos*\n"
        "• *correos hoy* — lista con relevancia\n"
        "• *correos no leídos*\n"
        "• *correos 20* — los últimos N\n\n"
        "🧵 *Conversaciones*\n"
        "• *hilo <tema>* — busca y resume un hilo\n\n"
        "↩️ *Responder*\n"
        "• *reply:<mensaje>* — responde el último correo recibido\n\n"
        "• *ayuda* — este menú"
    )
    await twilio.send_message(help_text)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def build_date_query(periodo: str) -> str:
    from datetime import datetime, timedelta
    periodo = periodo.lower().strip()
    today   = datetime.now()

    if periodo in ("hoy", "today", ""):
        return f"after:{today.strftime('%Y/%m/%d')}"
    elif periodo in ("semana", "week", "esta semana"):
        return f"after:{(today - timedelta(days=7)).strftime('%Y/%m/%d')}"
    elif periodo in ("mes", "month", "este mes"):
        return f"after:{(today - timedelta(days=30)).strftime('%Y/%m/%d')}"
    elif periodo in ("no leídos", "no leidos", "unread"):
        return "is:unread"
    else:
        return periodo


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "running"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)