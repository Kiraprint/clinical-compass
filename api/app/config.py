"""Конфигурация API (pydantic-settings). Префикс env: CC_"""

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Демо-секреты, запрещённые по умолчанию (F3). Чтобы стартовать с ними,
# нужно явно поставить CC_ALLOW_DEMO_SECRETS=true (демо/тест). prod-деплой as-is → fail.
_DEMO_SECRETS = {"dev-secret-change-me-0123456789abcdef", "demo-jwt-secret-0123456789abcdefghij"}
_DEMO_KEYS = {
    "compass-demo-data-key-do-not-use-in-prod-0123456789",
    "test-data-key-0123456789abcdef0123456789abcdef",
}


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
    bootstrap_password: str = ""  # CC_BOOTSTRAP_PASSWORD: для python -m app.bootstrap_user (оператор)
    qdrant_url: str = "http://127.0.0.1:6333"  # векторная БД ИИ-агента (docs/07)
    qdrant_collection: str = "kb"  # документы: открытый не-ПДн корпус (docs/08)
    embedder: str = "hash"  # hash | bge-m3 (docs/08 §4; по умолчанию офлайн-демо)
    depersonalize_ner: bool = False  # CC_DEPERSONALIZE_NER: NER-расширение ФИО-детекции (natasha, опционально, docs/12)
    data_key: str = ""  # CC_DATA_KEY: симметричный ключ pgcrypto для free-text ПДн (прод — KMS)
    # F3: инверсия fail-fast. По умолчанию демо-секреты запрещены; демо/тест
    # окружение явно ставит CC_ALLOW_DEMO_SECRETS=true. prod-деплой compose as-is
    # (с demo-jwt-secret) → Settings() падает при импорте → сервер не стартует.
    allow_demo_secrets: bool = False
    prod_mode: bool = False  # CC_PROD_MODE: дополнительные строгие проверки (KMS-класс data_key и т.д.)
    cookie_secure: bool = True  # CC_COOKIE_SECURE: Secure flag на httpOnly cookies (False для HTTP-демо)
    model_config = {"env_prefix": "CC_"}

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        # F3 (ФСТЭК №21 ИАФ, ГОСТ 56939): секреты аутентификации — вне git, достаточно энтропии.
        if len(self.jwt_secret) < 32:
            raise ValueError("CC_JWT_SECRET должен быть >= 32 байт (RFC 7518)")
        # Всегда (включая демо) — reject known demo secrets unless explicitly allowed.
        if self.jwt_secret in _DEMO_SECRETS and not self.allow_demo_secrets:
            raise ValueError(
                "CC_JWT_SECRET — демо-значение из git; для демо/теста поставьте "
                "CC_ALLOW_DEMO_SECRETS=true, для prod — сгенерируйте >=32 байт энтропии"
            )
        # Ключ шифрования ПДн: короткий ИЛИ известный демо-ключ запрещены без
        # явного демо-флага — иначе сильный JWT-секрет маскировал бы известный
        # ключ шифрования (ПДн шифровались бы ключом из git).
        if len(self.data_key) < 32 and not self.allow_demo_secrets:
            raise ValueError(
                "CC_DATA_KEY (>=32 байт) обязателен для шифрования ПДн; "
                "для демо/теста поставьте CC_ALLOW_DEMO_SECRETS=true"
            )
        if self.data_key in _DEMO_KEYS and not self.allow_demo_secrets:
            raise ValueError(
                "CC_DATA_KEY — демо-значение из git; сгенерируйте свой ключ "
                "(tools/gen-secrets.sh) или поставьте CC_ALLOW_DEMO_SECRETS=true для демо"
            )
        if self.prod_mode:
            if self.allow_demo_secrets:
                raise ValueError("CC_PROD_MODE несовместим с CC_ALLOW_DEMO_SECRETS")
        return self


settings = Settings()
