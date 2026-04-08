import json
from groq import AsyncGroq
from config import settings

client = AsyncGroq(api_key=settings.groq_api_key)

# Bloque de formato inyectado en todos los prompts
WA_FORMAT = """
FORMATO WHATSAPP — REGLAS ESTRICTAS:
✅ PERMITIDO:
  • *palabra* → negrita
  • _palabra_ → cursiva
  • Emojis al inicio de línea como separadores visuales
  • Listas con • o 1. 2. 3.
  • Líneas en blanco para separar secciones

❌ PROHIBIDO — si usas cualquiera de esto el mensaje se verá roto:
  • ## Títulos con almohadilla
  • **doble asterisco**
  • __doble guión bajo__
  • --- separadores
  • | tablas |
  • > citas
  • ``` bloques de código ```
  • Listas con guión: - item

EJEMPLO CORRECTO:
🔴 *Correo urgente*
De: Juan García
Asunto: Reunión mañana
_Requiere confirmar asistencia antes de las 5pm._

EJEMPLO INCORRECTO (NO hagas esto):
## Correo urgente
**De:** Juan García
- Requiere confirmar asistencia
"""


class GroqService:
    async def _chat(self, system: str, user: str, max_tokens: int = 1024, json_mode: bool = False) -> str:
        kwargs = dict(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()

    async def classify_email(self, email: dict) -> dict:
        """
        Clasifica relevancia y genera resumen corto.
        Devuelve JSON con 'relevancia' y 'resumen_corto'.
        """
        system = (
            "Eres un clasificador de correos. "
            "Responde ÚNICAMENTE con JSON válido, sin texto adicional, sin bloques de código.\n"
            "Formato exacto: {\"relevancia\": \"alta\", \"resumen_corto\": \"...\"}\n\n"
            "Valores de relevancia:\n"
            "• alta: requiere acción urgente, es de cliente, jefe o tiene fecha límite\n"
            "• media: informativo, requiere respuesta pero no urgente\n"
            "• baja: newsletter, promoción, notificación automática\n\n"
            "resumen_corto: máximo 25 palabras, qué trata el correo."
        )
        user = (
            f"De: {email['from']}\n"
            f"Asunto: {email['subject']}\n"
            f"Contenido: {email['body'][:1200] or email['snippet']}"
        )
        raw = await self._chat(system, user, max_tokens=150, json_mode=True)
        try:
            clean  = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            result = json.loads(clean)
            # Garantizar que siempre existan ambas claves
            return {
                "relevancia":    result.get("relevancia", "media"),
                "resumen_corto": result.get("resumen_corto") or email.get("snippet", "")[:120],
            }
        except Exception:
            return {"relevancia": "media", "resumen_corto": email.get("snippet", "")[:120]}

    async def summarize_emails(self, emails: list[dict], periodo: str = "hoy") -> str:
        """
        Clasifica cada correo individualmente y construye el resumen en orden estricto.
        """
        # Paso 1 — clasificar cada correo con Groq
        import asyncio
        classifications = await asyncio.gather(*[
            self.classify_email(e) for e in emails
        ])

        alta  = [(e, c) for e, c in zip(emails, classifications) if c["relevancia"] == "alta"]
        media = [(e, c) for e, c in zip(emails, classifications) if c["relevancia"] == "media"]
        baja  = [(e, c) for e, c in zip(emails, classifications) if c["relevancia"] == "baja"]

        # Paso 2 — construir el input estructurado para el LLM
        def fmt_email(e, c):
            remitente = e["from"].split("<")[0].strip() or e["from"]
            resumen   = c.get("resumen_corto") or e.get("snippet", "")[:120]
            return f"• *{remitente}* — {e['subject']}\n  {resumen}"

        secciones = [f"📬 *Resumen — {periodo}* | {len(emails)} correos"]

        if alta:
            secciones.append("\n🔴 *Alta prioridad*")
            secciones.extend(fmt_email(e, c) for e, c in alta)

        if media:
            secciones.append("\n🟡 *Media prioridad*")
            secciones.extend(fmt_email(e, c) for e, c in media)

        if baja:
            secciones.append(f"\n🟢 *Baja prioridad*\n{len(baja)} correos sin acción requerida.")

        resumen_base = "\n".join(secciones)

        # Paso 3 — pedir al LLM solo las acciones pendientes
        system = (
            "Eres un asistente ejecutivo. Se te da un resumen de correos ya clasificados. "
            "Tu única tarea es escribir la sección de acciones pendientes.\n\n"
            + WA_FORMAT +
            "\nEscribe ÚNICAMENTE esto, sin repetir el resumen:\n\n"
            "✅ *Acciones pendientes*\n"
            "1. [acción concreta]\n"
            "2. [acción concreta]\n"
            "3. [acción concreta si aplica]\n\n"
            "Basa las acciones solo en los correos de alta y media prioridad. "
            "Si no hay acciones claras, escribe: ✅ *Sin acciones urgentes por ahora.*"
        )
        alta_media_txt = "\n".join(
            f"De: {e['from']} | Asunto: {e['subject']} | {c['resumen_corto']}"
            for e, c in (alta + media)
        ) or "Ninguno"

        acciones = await self._chat(system, alta_media_txt, max_tokens=300)

        return resumen_base + "\n\n" + acciones

    async def summarize_thread(self, emails: list[dict], topic: str) -> str:
        """
        Resume una conversación completa (hilo), formato WhatsApp.
        """
        system = (
            "Eres un asistente que analiza hilos de correo electrónico.\n\n"
            + WA_FORMAT +
            """
ESTRUCTURA QUE DEBES SEGUIR:

🧵 *Conversación: [tema]*

👥 *Participantes:* [nombres o emails]

📋 *De qué trata:*
[2-4 líneas explicando el contexto y qué se ha discutido]

📍 *Estado actual:*
[1-2 líneas sobre dónde está la conversación ahora]

⏳ *Pendiente:*
[Qué falta resolver o quién debe responder, si aplica]

Usa datos reales. Si no hay pendiente, omite esa sección.
"""
        )
        lines = [
            f"[{e['date']}]\nDe: {e['from']}\n{e['body'][:700] or e['snippet']}"
            for e in emails
        ]
        user = f"Hilo sobre: {topic}\n\n" + "\n\n─────\n\n".join(lines)
        return await self._chat(system, user, max_tokens=1200)

    async def polish_reply(self, draft: str, original_email: dict) -> str:
        """
        Convierte el borrador en una respuesta profesional lista para enviar por email.
        Este texto va directo al correo — NO necesita formato WhatsApp.
        """
        system = (
            "Eres un asistente de redacción de correos electrónicos. "
            "El usuario te da un borrador corto y debes convertirlo en una respuesta "
            "completa, profesional y en el mismo idioma que el correo original.\n\n"
            "Reglas:\n"
            "• Mantén el tono apropiado al contexto (formal o informal)\n"
            "• No inventes información que no esté en el borrador o en el correo original\n"
            "• Incluye saludo y despedida apropiados\n"
            "• No incluyas marcadores como [Nombre] o [Tu firma]\n"
            "• Responde solo el texto del correo, sin explicaciones adicionales"
        )
        user = (
            f"Correo original:\n"
            f"De: {original_email['from']}\n"
            f"Asunto: {original_email['subject']}\n"
            f"Contenido:\n{original_email['body'][:1000]}\n\n"
            f"─────\n"
            f"Mi idea de respuesta: {draft}"
        )
        return await self._chat(system, user, max_tokens=800)

    async def free_chat(self, message: str, context: str = "") -> str:
        """
        Chat libre sobre correos, respuesta en formato WhatsApp.
        """
        system = (
            "Eres un asistente personal de correo electrónico accesible por WhatsApp. "
            "El usuario te hace preguntas sobre sus correos.\n\n"
            "REGLAS ESTRICTAS:\n"
            "• Responde SOLO con lo que encuentres en los correos del contexto\n"
            "• Si no hay información relevante, di exactamente: "
            "'No encontré correos relacionados con ese tema en tu bandeja reciente.'\n"
            "• NO hagas preguntas de seguimiento\n"
            "• NO inventes datos ni supongas información\n"
            "• NO muestres la lista completa de correos\n"
            "• Máximo 5 líneas de respuesta\n\n"
            + WA_FORMAT
            + ("\n\nCorreos recientes (usa solo estos datos):\n" + context if context else
               "\n\nNo hay correos en contexto.")
        )
        return await self._chat(system, message, max_tokens=300)