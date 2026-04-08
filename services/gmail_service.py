import os
import base64
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailService:
    def __init__(self):
        self._service = None
        self._last_history_id: str | None = None  # persiste entre notificaciones

    def _get_service(self):
        if self._service:
            return self._service

        creds = None

        # En Railway el token viene como variable de entorno (JSON string)
        # En local se lee desde el archivo token.json
        token_content = os.environ.get("GMAIL_TOKEN_CONTENT")
        if token_content:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_content), SCOPES
            )
        else:
            try:
                creds = Credentials.from_authorized_user_file(
                    settings.gmail_token_json, SCOPES
                )
            except Exception:
                pass

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Sin token válido: intentar OAuth flow con credentials.json
                # En Railway, GMAIL_CREDENTIALS_CONTENT tiene el JSON del credentials.json
                # En local, se lee desde el archivo configurado en .env
                creds_content = os.environ.get("GMAIL_CREDENTIALS_CONTENT")
                if creds_content:
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
                        tmp.write(creds_content)
                        tmp_path = tmp.name
                    flow = InstalledAppFlow.from_client_secrets_file(tmp_path, SCOPES)
                    os.unlink(tmp_path)
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        settings.gmail_credentials_json, SCOPES
                    )
                creds = flow.run_local_server(port=8080)

            if not token_content:
                with open(settings.gmail_token_json, "w") as f:
                    f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # ------------------------------------------------------------------ #
    #  PUSH — solo para correos nuevos vía Pub/Sub                        #
    # ------------------------------------------------------------------ #

    async def fetch_from_pubsub(self, pubsub_data: dict) -> dict | None:
        """
        Decodifica la notificación de Pub/Sub y obtiene el correo completo.
        Usa el historyId anterior como punto de partida para no perder mensajes.
        """
        try:
            encoded    = pubsub_data.get("message", {}).get("data", "")
            decoded    = json.loads(base64.b64decode(encoded).decode("utf-8"))
            new_history_id = decoded.get("historyId")

            # Usamos el historyId anterior como startHistoryId
            # Si no tenemos uno previo, usamos el que viene en la notificación - 1
            start_id = self._last_history_id or str(int(new_history_id) - 1)

            print(f"[fetch_from_pubsub] startHistoryId={start_id} newHistoryId={new_history_id}")

            service = self._get_service()

            # Buscar tanto messageAdded como labelsAdded (Gmail notifica ambos)
            history = service.users().history().list(
                userId=settings.gmail_user_id,
                startHistoryId=start_id,
            ).execute()

            # Actualizar el historyId para la próxima notificación
            self._last_history_id = new_history_id

            # Recolectar IDs únicos de cualquier mensaje mencionado en el historial
            message_ids = []
            seen = set()
            for record in history.get("history", []):
                for key in ("messagesAdded", "labelsAdded"):
                    for entry in record.get(key, []):
                        mid = entry["message"]["id"]
                        if mid not in seen:
                            seen.add(mid)
                            message_ids.append(mid)

            print(f"[fetch_from_pubsub] Mensajes en historial: {len(message_ids)}")

            if not message_ids:
                return None

            # Filtrar solo los que estén en INBOX y sean de hoy
            for mid in message_ids:
                try:
                    msg = service.users().messages().get(
                        userId=settings.gmail_user_id,
                        id=mid,
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    ).execute()
                    labels = msg.get("labelIds", [])
                    if "INBOX" in labels and "SENT" not in labels:
                        print(f"[fetch_from_pubsub] Correo válido encontrado: {mid}")
                        return self._parse_message(service, mid)
                except Exception:
                    continue

            print("[fetch_from_pubsub] Ningún mensaje del historial es de INBOX")
            return None

        except Exception as e:
            import traceback
            print(f"[GmailService] fetch_from_pubsub error: {e}")
            print(traceback.format_exc())
            return None

    # ------------------------------------------------------------------ #
    #  API REST — consultas en tiempo real para resumen, hilos, búsqueda  #
    # ------------------------------------------------------------------ #

    async def fetch_emails(
        self,
        query: str = "in:inbox",
        max_results: int = 20,
        snippet_only: bool = False,
    ) -> list[dict]:
        """
        Consulta directa a Gmail API. No depende del watch ni del estado local.

        query acepta cualquier sintaxis de búsqueda de Gmail:
          - "in:inbox after:2024/01/01"
          - "is:unread"
          - "from:alguien@example.com"
          - "subject:proyecto"

        snippet_only=True devuelve solo metadatos + snippet (más rápido),
        útil para el contexto del chat libre sin necesitar el cuerpo completo.
        """
        service = self._get_service()

        result = service.users().messages().list(
            userId=settings.gmail_user_id,
            maxResults=max_results,
            q=query,
        ).execute()

        messages = result.get("messages", [])
        emails   = []

        for msg in messages:
            try:
                if snippet_only:
                    emails.append(self._parse_snippet(service, msg["id"]))
                else:
                    emails.append(self._parse_message(service, msg["id"]))
            except Exception as e:
                print(f"[GmailService] Error parseando {msg['id']}: {e}")

        return emails

    async def fetch_thread(self, thread_id: str) -> list[dict]:
        """
        Obtiene todos los mensajes de un hilo ordenados cronológicamente.
        Útil para resumir conversaciones completas.
        """
        service = self._get_service()

        thread = service.users().threads().get(
            userId=settings.gmail_user_id,
            id=thread_id,
            format="full",
        ).execute()

        return [
            self._parse_message(service, msg["id"])
            for msg in thread.get("messages", [])
        ]

    # ------------------------------------------------------------------ #
    #  Envío de respuestas                                                 #
    # ------------------------------------------------------------------ #

    async def send_reply(self, original_email: dict, reply_body: str):
        service = self._get_service()

        # Extraer solo el email del campo from, que puede venir como:
        # "Nombre Apellido <email@domain.com>" o simplemente "email@domain.com"
        raw_from = original_email["from"]
        if "<" in raw_from and ">" in raw_from:
            to_address = raw_from.split("<")[1].split(">")[0].strip()
        else:
            to_address = raw_from.strip()

        message = MIMEMultipart()
        message["to"]          = to_address
        message["subject"]     = f"Re: {original_email['subject']}"
        message["In-Reply-To"] = original_email["id"]
        message["References"]  = original_email["id"]
        message.attach(MIMEText(reply_body, "plain"))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(
            userId=settings.gmail_user_id,
            body={"raw": raw, "threadId": original_email["thread_id"]},
        ).execute()

    # ------------------------------------------------------------------ #
    #  Parsers internos                                                    #
    # ------------------------------------------------------------------ #

    def _parse_message(self, service, message_id: str) -> dict:
        msg     = service.users().messages().get(
            userId=settings.gmail_user_id,
            id=message_id,
            format="full",
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

        return {
            "id":        message_id,
            "thread_id": msg["threadId"],
            "from":      headers.get("From", ""),
            "to":        headers.get("To", ""),
            "subject":   headers.get("Subject", "(sin asunto)"),
            "date":      headers.get("Date", ""),
            "body":      self._extract_body(msg["payload"]),
            "snippet":   msg.get("snippet", ""),
        }

    def _parse_snippet(self, service, message_id: str) -> dict:
        """Versión ligera: solo metadatos + snippet, sin descargar el cuerpo."""
        msg     = service.users().messages().get(
            userId=settings.gmail_user_id,
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

        return {
            "id":        message_id,
            "thread_id": msg["threadId"],
            "from":      headers.get("From", ""),
            "subject":   headers.get("Subject", "(sin asunto)"),
            "date":      headers.get("Date", ""),
            "snippet":   msg.get("snippet", ""),
            "body":      "",
        }

    def _extract_body(self, payload: dict) -> str:
        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain":
                    data = part["body"].get("data", "")
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                if part["mimeType"] == "multipart/alternative":
                    return self._extract_body(part)

        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    # ------------------------------------------------------------------ #
    #  Setup del watch Pub/Sub (correr una vez en setup.py)               #
    # ------------------------------------------------------------------ #

    def setup_push_notifications(self):
        service = self._get_service()
        topic   = f"projects/{settings.google_cloud_project}/topics/{settings.pubsub_topic}"
        result  = service.users().watch(
            userId=settings.gmail_user_id,
            body={"labelIds": ["INBOX"], "topicName": topic},
        ).execute()
        print(f"[GmailService] Push watch activo hasta: {result.get('expiration')}")
        return result