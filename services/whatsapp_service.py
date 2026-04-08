import httpx
from config import settings


class WhatsAppService:
    """
    Envía mensajes a través del bridge whatsapp-web.js corriendo en el VPS.
    Reemplaza a TwilioService — misma interfaz, distinto transporte.
    """

    async def send_message(self, body: str):
        parts = self._split(body)
        async with httpx.AsyncClient(timeout=15) as http:
            for part in parts:
                try:
                    response = await http.post(
                        f"{settings.wa_bridge_url}/send",
                        json={"to": settings.wa_my_number, "body": part},
                        headers={"x-api-secret": settings.wa_bridge_secret},
                    )
                    response.raise_for_status()
                except Exception as e:
                    print(f"[WhatsAppService] Error enviando mensaje: {e}")

    def _split(self, text: str) -> list[str]:
        """Divide el texto en partes de máximo 1500 caracteres cortando en saltos de línea."""
        MAX = 1500
        if len(text) <= MAX:
            return [text]

        parts = []
        lines = text.splitlines(keepends=True)
        current = ""

        for line in lines:
            if len(current) + len(line) > MAX:
                if current:
                    parts.append(current.rstrip())
                current = line
            else:
                current += line

        if current.strip():
            parts.append(current.rstrip())

        return parts