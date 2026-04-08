from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Groq
    groq_api_key: str
    groq_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_from: str   # "whatsapp:+14155238886" (sandbox)
    twilio_whatsapp_to: str     # "whatsapp:+51xxxxxxxxx" (tu número)
    twilio_content_sid: str     # "HXxxxx..." — SID del Content Template

    # Gmail OAuth2
    gmail_credentials_json: str  # path al credentials.json de Google
    gmail_token_json: str = "token.json"
    gmail_user_id: str = "me"

    # Google Cloud Pub/Sub
    google_cloud_project: str
    pubsub_topic: str = "gmail-notifications"
    pubsub_subscription: str = "gmail-notifications-sub"

    class Config:
        env_file = ".env"


settings = Settings()