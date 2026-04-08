from typing import Optional


class SessionService:
    """
    Estado mínimo en memoria.
    Ya no necesitamos guardar todos los correos del día porque
    ahora los consultamos en tiempo real desde la Gmail API.
    Solo guardamos el último correo recibido por Push para poder
    hacer reply sin necesidad de buscarlo de nuevo.
    """

    def __init__(self):
        self._last_email: Optional[dict] = None

    def set_last_email(self, email: dict):
        self._last_email = email

    def get_last_email(self) -> Optional[dict]:
        return self._last_email
