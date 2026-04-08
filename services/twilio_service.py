import json
from twilio.rest import Client
from config import settings

client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

MAX_LENGTH = 1500  # margen de seguridad bajo el límite de 1600 de Twilio


class TwilioService:
    async def send_message(self, body: str):
        """
        Envía un mensaje de WhatsApp. Si supera los 1500 caracteres,
        lo divide en partes respetando los saltos de línea.
        """
        parts = self._split(body)
        for part in parts:
            try:
                client.messages.create(
                    from_=settings.twilio_whatsapp_from,
                    content_sid=settings.twilio_content_sid,
                    content_variables=json.dumps({"content": part}),
                    to=settings.twilio_whatsapp_to,
                )
            except Exception as e:
                print(f"[TwilioService] Error enviando mensaje: {e}")

    def _split(self, text: str) -> list[str]:
        """Divide el texto en partes de máximo MAX_LENGTH caracteres,
        cortando siempre en saltos de línea para no partir frases."""
        if len(text) <= MAX_LENGTH:
            return [text]

        parts = []
        lines = text.splitlines(keepends=True)
        current = ""

        for line in lines:
            if len(current) + len(line) > MAX_LENGTH:
                if current:
                    parts.append(current.rstrip())
                current = line
            else:
                current += line

        if current.strip():
            parts.append(current.rstrip())

        return parts