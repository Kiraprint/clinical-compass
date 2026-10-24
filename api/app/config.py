"""Конфигурация API (pydantic-settings). Префикс env: CC_"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

# Демо-секреты, запрещённые по умолчанию (F3). Чтобы стартовать с ними,
# нужно явно поставить CC_ALLOW_DEMO_SECRETS=true (демо/тест). prod-деплой as-is → fail.
_DEMO_SECRETS = {"dev-secret-change-me-0123456789abcdef", "demo-jwt-secret-0123456789abcdefghij"}
_DEMO_KEYS = {
    "compass-demo-data-key-do-not-use-in-prod-0123456789",
    "test-data-key-0123456789abcdef0123456789abcdef",
}
_DEMO_EMIAS_CLIENTS = {"compass"}
_DEMO_EMIAS_SECRETS = {"compass-secret"}


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://compass:compass@127.0.0.1:5432/compass"
    # Пул async-соединений на процесс API (см. app.db.validate_pool_budget).
    # Спрос процесса = pool_size + max_overflow; бюджет всей инсталляции —
    # CC_DB_MAX_CONNECTIONS (max_connections Postgres делят все воркеры, CLI и psql).
    # Дефолты 20+20 рассчитаны максимум на 2 воркера в бюджете 100.
    db_pool_size: int = 20  # CC_DB_POOL_SIZE: постоянных соединений на процесс
    db_max_overflow: int = 20  # CC_DB_MAX_OVERFLOW: пиковых сверх pool_size
    db_pool_timeout: int = 30  # CC_DB_POOL_TIMEOUT: сек ожидания свободного соединения
    db_max_connections: int = 100  # CC_DB_MAX_CONNECTIONS: потолок Postgres для инсталляции
    # Фоновые процессы (reminders-worker, kb-enrichment) держат свой маленький
    # пул: им не нужен веер соединений под HTTP-нагрузку, но их спрос обязан
    # попадать в тот же бюджет max_connections (см. db.validate_pool_budget).
    db_bg_pool_size: int = 2  # CC_DB_BG_POOL_SIZE: соединений на фоновый процесс
    db_bg_max_overflow: int = 1  # CC_DB_BG_MAX_OVERFLOW: пиковых сверх пула (тик держит лок-соединение + сессию)
    db_background_processes: int = 2  # CC_DB_BACKGROUND_PROCESSES: их число в инсталляции
    jwt_secret: str = "dev-secret-change-me-0123456789abcdef"  # >= 32 байт (RFC 7518)
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 7
    rate_max: int = 5  # попыток захода на /auth/login за окно (перебор 2FA)
    rate_window: int = 60  # сек
    rate_ip_max: int = 30  # CC_RATE_IP_MAX: неудачных попыток на IP за окно (любые email)
    # Rate limiter логинов (docs/03 §12): memory — словари процесса (бюджет
    # умножается на WEB_CONCURRENCY и реплики), redis — общий атомарный счётчик
    # в Redis, auto — redis при заданном CC_REDIS_URL и живом PING, иначе memory.
    rate_backend: str = "auto"  # CC_RATE_BACKEND: auto | memory | redis
    redis_url: str = ""  # CC_REDIS_URL: пусто → in-process memory (бюджет на процесс)
    # CC_RATE_REDIS_TIMEOUT: сек на socket/connect. Мёртвый Redis не должен
    # задерживать логин: по таймауту limiter деградирует в memory (fail-open).
    rate_redis_timeout: float = Field(default=0.3, gt=0)
    push_enabled: bool = True
    vapid_private_key: str = ""  # base64(PKCS8 PEM); пусто → генерируется в памяти (демо)
    fcm_service_account_b64: str = ""  # base64(JSON service account Firebase); пусто → FCM выключен
    bootstrap_password: str = ""  # CC_BOOTSTRAP_PASSWORD: для python -m app.bootstrap_user (оператор)
    qdrant_url: str = "http://127.0.0.1:6333"  # векторная БД ИИ-агента (docs/07)
    qdrant_collection: str = "kb"  # документы: открытый не-ПДн корпус (docs/08)
    # Профиль эмбеддера (docs/08 §4): hash — офлайн-демо/тесты (dense 512);
    # bge-m3 — прод (dense 1024; нужен extra `embeddings` и переиндексация KB).
    embedder: str = "hash"
    embedder_model: str = ""  # CC_EMBEDDER_MODEL: id модели; пусто → дефолт профиля
    embedder_device: str = "auto"  # CC_EMBEDDER_DEVICE: auto | cpu | cuda[:N]
    embedder_batch_size: int = 8  # CC_EMBEDDER_BATCH_SIZE: батч энкодера
    embedder_max_length: int = 2048  # CC_EMBEDDER_MAX_LENGTH: токенов на текст
    # CC_KB_MIN_SCORE: порог «релевантного ответа нет» для score лучшего чанка.
    # Пусто → дефолт профиля (PROFILES[...].default_min_score, docs/08 §4):
    # шкала косинуса у моделей разная, единый порог ненадёжен.
    kb_min_score: float | None = None
    # CC_RERANKER: off | id кросс-энкодера (BAAI/bge-reranker-v2-m3 и др.).
    # Требует extra `rerank`; без него — предупреждение и порядок RRF (docs/08 §4).
    reranker: str = "off"
    # CC_RETRIEVAL_CANDIDATES: глубина пула до реранка/отсечки (0 → max(limit*3, 10)).
    retrieval_candidates: int = 0
    # CC_RRF_K: константа RRF-слияния dense+sparse (Qdrant RrfQuery, docs/08 §4).
    rrf_k: int = 60
    # CC_KB_CORPUS_TTL_SECONDS: TTL кеша корпуса KB (kb_seed.build_corpus) в
    # процессе API. In-process писателей drugs нет (seed — отдельный CLI, он
    # зовёт clear_corpus_cache), поэтому TTL — единственная runtime-инвалидация.
    # 0 → кеш выключен.
    kb_corpus_ttl_seconds: float = 60.0
    # CC_ASSISTANT_LLM_URL: OpenAI-совместимый /v1 (llama-server, vLLM, Ollama).
    # Пусто → LLM-роутинг топиков выключен (поиск по всем разделам). LLM получает
    # только очищенный от ПДн запрос и выбирает тулзу search_<раздел> (docs/12).
    assistant_llm_url: str = ""
    assistant_llm_model: str = "local"  # CC_ASSISTANT_LLM_MODEL: имя модели на сервере
    assistant_llm_timeout: float = 10.0  # CC_ASSISTANT_LLM_TIMEOUT: сек на выбор тулзы
    # CC_ASSISTANT_LLM_API_KEY: Bearer-токен OpenAI-совместимого шлюза (omniroute).
    # Пусто → заголовок Authorization не отправляется (llama-server без ключа).
    assistant_llm_api_key: str = ""
    # CC_ASSISTANT_THREAD_LIMIT: потолок одновременных прогонов RAG-агента на
    # процесс. У агента свой лимитер (services.assistant_chat.AGENT_LIMITER), а не
    # общий пул AnyIO на 40 потоков: всплеск ассистента ждёт на лимитере, оставляя
    # пул push/эмбеддингам/Qdrant других ручек (docs/03 §12).
    assistant_thread_limit: int = Field(default=8, ge=1)
    # CC_ASSISTANT_DEADLINE_S: дедлайн на весь прогон агента — ожидание лимитера +
    # retrieval + LLM. По истечении — 503 ASSISTANT_TIMEOUT (Retry-After: 5).
    assistant_deadline_s: float = Field(default=60.0, gt=0)
    # FR-31: анализ переписки с пациентом (факты-черновики). Выключен по умолчанию.
    # Требует и этот флаг, и CC_ASSISTANT_LLM_URL: текст переписки уходит в LLM
    # только при явном включении и (рекомендуется) локальной модели — docs/17, docs/10.
    chat_insights_enabled: bool = False  # CC_CHAT_INSIGHTS_ENABLED
    # CC_DEPERSONALIZE_NER: off | natasha | rubert (docs/12). true/1 = natasha
    # (обратная совместимость). rubert — redmadrobot-rnd/rubert-base-pii-ner:
    # Apache-2.0, FIO F1 0.933 на redmadrobot-rnd/pii_benchmark (bench/ner).
    depersonalize_ner: str = "off"
    ner_model: str = "redmadrobot-rnd/rubert-base-pii-ner"  # CC_NER_MODEL
    data_key: str = ""  # CC_DATA_KEY: симметричный ключ pgcrypto для free-text ПДн (прод — KMS)
    # F3: инверсия fail-fast. По умолчанию демо-секреты запрещены; демо/тест
    # окружение явно ставит CC_ALLOW_DEMO_SECRETS=true. prod-деплой compose as-is
    # (с demo-jwt-secret) → Settings() падает при импорте → сервер не стартует.
    allow_demo_secrets: bool = False
    prod_mode: bool = False  # CC_PROD_MODE: дополнительные строгие проверки (KMS-класс data_key и т.д.)
    cookie_secure: bool = True  # CC_COOKIE_SECURE: Secure flag на httpOnly cookies (False для HTTP-демо)
    # CC_REQUIRE_OTP: требовать 2FA-код при входе CLINICIAN/ADMIN. false — отключить
    # проверку (демо/локально), totp_secret всё равно хранится в БД — развернуть
    # версию с OTP можно одним флагом. prod_mode требует require_otp=true.
    require_otp: bool = True
    # FR-24: период тика воркера напоминаний (сек). Отдельного шедулера в MVP нет:
    # `python -m app.reminders_worker` — отдельный процесс, как kb-enrichment.
    reminder_tick_seconds: int = 60  # CC_REMINDER_TICK_SECONDS
    # --- FR-25: интеграция с ЕМИАС (docs/04 §3.1, docs/09, docs/10 R17-R19) -----
    # Egress по умолчанию ВЫКЛЮЧЕН: без CC_EMIAS_ENABLED=true ни один запрос во
    # внешний контур не уходит. Белый список хостов — netloc из CC_EMIAS_BASE_URL.
    emias_enabled: bool = False
    emias_base_url: str = "http://127.0.0.1:8090"  # локальный мок; прод — https ЕМИАС
    emias_client_id: str = "compass"  # client_credentials, секрет — вне git
    emias_client_secret: str = "compass-secret"
    emias_allow_insecure: bool = False  # http разрешён только для мока/локальной сети
    emias_timeout: float = 10.0  # сек на HTTP-запрос
    emias_max_attempts: int = 3  # 429/5xx/таймаут: до 3 попыток с backoff
    emias_max_response_bytes: int = 4 * 1024 * 1024  # жёсткий кап тела ответа
    emias_snils_pepper: str = ""  # соль sha256(СНИЛС); пусто → CC_DATA_KEY
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
        # FR-25: демо-креды мока (compose emias-mock) недопустимы в проде —
        # иначе интеграция уходила бы в чужой/мок-контур с известным секретом.
        if self.prod_mode and self.emias_enabled:
            if (
                self.emias_client_secret in _DEMO_EMIAS_SECRETS
                or self.emias_client_id in _DEMO_EMIAS_CLIENTS
            ):
                raise ValueError(
                    "CC_PROD_MODE: демо-креды ЕМИАС (CC_EMIAS_CLIENT_ID/SECRET) запрещены"
                )
            if self.emias_allow_insecure:
                raise ValueError("CC_PROD_MODE требует CC_EMIAS_ALLOW_INSECURE=false")
        if self.prod_mode:
            if self.allow_demo_secrets:
                raise ValueError("CC_PROD_MODE несовместим с CC_ALLOW_DEMO_SECRETS")
            if not self.require_otp:
                raise ValueError("CC_PROD_MODE требует CC_REQUIRE_OTP=true (2FA обязателен в проде)")
        return self


settings = Settings()
