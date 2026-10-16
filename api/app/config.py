"""Конфигурация API (pydantic-settings). Префикс env: CC_"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://compass:compass@127.0.0.1:5432/compass"
    jwt_secret: str = "dev-secret-change-me-0123456789abcdef"  # >= 32 байт (RFC 7518)
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 7
    rate_max: int = 5  # попыток захода на /auth/login за окно (перебор 2FA)
    rate_window: int = 60  # сек
    push_enabled: bool = True
    vapid_private_key: str = ""  # base64(PKCS8 PEM); пусто → генерируется в памяти (демо)
    fcm_service_account_b64: str = ""  # base64(JSON service account Firebase); пусто → FCM выключен
    qdrant_url: str = "http://127.0.0.1:6333"  # векторная БД ИИ-агента (docs/07)
    qdrant_collection: str = "kb"  # документы: открытый не-ПДн корпус (docs/08)
    model_config = {"env_prefix": "CC_"}


settings = Settings()