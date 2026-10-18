"""Конфигурация API (pydantic-settings). Префикс env: CC_"""

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Демо-секреты, запрещённые в prod-режиме (CC_PROD_MODE=true).
_DEMO_SECRETS = {"dev-secret-change-me-0123456789abcdef", "demo-jwt-secret-0123456789abcdefghij"}


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
    embedder: str = "hash"  # hash | bge-m3 (docs/08 §4; по умолчанию офлайн-демо)
    data_key: str = ""  # CC_DATA_KEY: симметричный ключ pgcrypto для free-text ПДн (прод — KMS)
    prod_mode: bool = False  # CC_PROD_MODE: fail-fast на демо-секретах/пустом data_key
    model_config = {"env_prefix": "CC_"}

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        # F3 (ФСТЭК №21 ИАФ, ГОСТ 56939): секреты аутентификации — вне git, достаточно энтропии.
        if len(self.jwt_secret) < 32:
            raise ValueError("CC_JWT_SECRET должен быть >= 32 байт (RFC 7518)")
        if self.prod_mode:
            if self.jwt_secret in _DEMO_SECRETS:
                raise ValueError("CC_PROD_MODE: CC_JWT_SECRET не должен быть демо-значением из git")
            if len(self.data_key) < 32:
                raise ValueError("CC_PROD_MODE: CC_DATA_KEY (>=32 байт) обязателен для шифрования ПДн")
        return self


settings = Settings()
