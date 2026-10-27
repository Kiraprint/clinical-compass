"""Конфигурация API (pydantic-settings). Префикс env: CC_"""

from pydantic import Field, field_validator, model_validator
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
    # CC_RERANK_WINDOW: ширина окна чанка, которое видит кросс-энкодер
    # (0 — начало чанка; 700 — лучшее по словам запроса окно, docs/08 §4.8).
    rerank_window: int = 0
    # CC_RETRIEVAL_CANDIDATES: глубина пула до реранка/отсечки (0 → max(limit*3, 10)).
    retrieval_candidates: int = 0
    # CC_RRF_K: константа RRF-слияния dense+sparse (Qdrant RrfQuery, docs/08 §4).
    # Не задана (или <=0) → историческая серверная форма FusionQuery(Fusion.RRF):
    # k берёт Qdrant (дефолт 2), выдача hash-профиля байт-в-байт как на main.
    # k=60 — часть ЯВНОГО retrieval-профиля (CC_RRF_K=60), а не дефолт.
    # `CC_RRF_K=` (пусто) = «не задана» (см. _rrf_k_unset).
    rrf_k: int | None = None
    # CC_KB_CORPUS_TTL_SECONDS: TTL кеша корпуса KB (kb_seed.build_corpus) в
    # процессе API. In-process писателей drugs нет (seed — отдельный CLI, он
    # зовёт clear_corpus_cache), поэтому TTL — единственная runtime-инвалидация.
    # 0 → кеш выключен.
    kb_corpus_ttl_seconds: float = 60.0
    # CC_KB_CHUNK_CHARS / CC_KB_CHUNK_OVERLAP: нарезка чанков КБ — общий
    # контракт kb_seed (сид) и enrichment/index.py (гайдлайны ВОЗ, статьи).
    # 1800/120 — исторический дефолт; 600/120 измеренно поднимает Hit@3
    # поиска (bench/REPORT-retrieval.md §6), но требует переиндексации.
    kb_chunk_chars: int = 1800
    kb_chunk_overlap: int = 120
    # CC_ASSISTANT_LLM_URL: OpenAI-совместимый /v1 (llama-server, vLLM, Ollama).
    # Пусто → LLM-роутинг топиков выключен (поиск по всем разделам). LLM получает
    # только очищенный от ПДн запрос и выбирает тулзу search_<раздел> (docs/12).
    assistant_llm_url: str = ""
    assistant_llm_model: str = "local"  # CC_ASSISTANT_LLM_MODEL: имя модели на сервере
    # CC_ASSISTANT_LLM_TIMEOUT: сек на выбор тулзы роутером. Reasoning-модели
    # (home-stable → ds/deepseek-v4-flash) тратят 17-18 с только на tool_call и
    # игнорируют enable_thinking=false — 10 с гарантировали fallback (docs/17 §8).
    assistant_llm_timeout: float = 120.0
    # CC_ASSISTANT_LLM_ANSWER_TIMEOUT: сек на генерацию ответа по сниппетам.
    # Замер того же шлюза: 44-82 с (reasoning съедает 350-1300 токенов бюджета),
    # поэтому 25 с давали детерминированный build_rag_answer вместо ответа.
    # Таймаут → answer_kind="fragments", llm_reason="timeout", не ошибка API.
    assistant_llm_answer_timeout: float = 120.0
    # CC_ASSISTANT_LLM_REASONING_EFFORT: `reasoning_effort` **верхнего уровня**
    # (внутри chat_template_kwargs шлюзы его не читают). `none` — без CoT:
    # reasoning-контента 0 символов и первый токен 6.9-17.6 с против 12-25 с и
    # 185-378 символов рассуждений; `low` хуже (26.8 с) — docs/17 §8.
    assistant_llm_reasoning_effort: str = "none"
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
    assistant_deadline_s: float = Field(default=150.0, gt=0)
    # FR-31: анализ переписки с пациентом (факты-черновики). Выключен по умолчанию.
    # Требует и этот флаг, и CC_ASSISTANT_LLM_URL: текст переписки уходит в LLM
    # только при явном включении и (рекомендуется) локальной модели — docs/17, docs/10.
    chat_insights_enabled: bool = False  # CC_CHAT_INSIGHTS_ENABLED
    # CC_CHAT_INSIGHTS_TIMEOUT: таймаут одного вызова при извлечении фактов.
    # Живой шлюз отвечает 12–13 с, прежний общий 30-секундный лимит давал
    # ложные 503; в chat_insights один ретрай на таймаут (docs/04 FR-31).
    chat_insights_timeout: float = Field(default=60.0, gt=0)
    # FR-37: анамнез пациента (черновик LLM + сохранение врачом). Выключен по
    # умолчанию: и хранение, и формирование черновика недоступны, пока явно не
    # включено. Для POST …/anamnesis/draft дополнительно нужен CC_ASSISTANT_LLM_URL.
    anamnesis_enabled: bool = False  # CC_ANAMNESIS_ENABLED
    # CC_ANAMNESIS_TIMEOUT: таймаут одного вызова LLM при формировании черновика.
    anamnesis_timeout: float = Field(default=60.0, gt=0)
    # docs/18 §9 (FR-38…FR-41): клинические сессии врача. ИИ-блок (аудио,
    # расшифровка, черновик протокола) — PR P3 и по умолчанию ВЫКЛЮЧЕН: календарь,
    # ручной протокол, подпись и передача в ЕМИАС работают без него.
    session_ai_enabled: bool = False  # CC_SESSION_AI_ENABLED
    # CC_SESSION_AUDIO_MAX_MB: потолок аудио на сессию (413, PR P3).
    session_audio_max_mb: int = Field(default=200, ge=1)
    # CC_SESSION_AI_TIMEOUT: сек на один шаг ИИ-конвейера — вызов сайдкара
    # `/v1/transcribe` (аудио приёма бывает на час) и черновик протокола в LLM.
    # Отдельно от CC_ML_TIMEOUT (5 с: маскирование ПДн ждёт короткий таймаут).
    session_ai_timeout: float = Field(default=600.0, gt=0)
    # CC_SESSION_AI_SOURCES (FR-43): сколько чанков KB искать для обоснования
    # черновика протокола (docs/18 §5.3–5.4). 0 — не ходить в KB вовсе
    # (черновик без источников, needs_review sources_unavailable).
    session_ai_sources: int = Field(default=12, ge=0, le=50)
    # FR-44 (docs/18 §10): автодрафт — черновик протокола создаётся тем же кодом
    # сразу после успешной расшифровки, без HTTP-запроса врача. false — только
    # ручной POST …/protocol/draft. Сбой автодрафта расшифровку не ломает
    # (`ai_status` остаётся `transcribed`, код — в `ai_error`).
    session_ai_autodraft: bool = True  # CC_SESSION_AI_AUTODRAFT
    # FR-44: диалог по сессии (§10). Потолок одного сообщения врача; всё, что
    # длиннее, — 422 MESSAGE_TOO_LONG, а не обрезка «молча».
    session_chat_message_chars: int = Field(default=2000, ge=1, le=8000)
    # Окно истории, которое уходит в модель: последние N реплик (свежее важнее).
    session_chat_history: int = Field(default=10, ge=0, le=50)
    # Потолок реплик врача на сессию: диалог — разбор приёма, а не общий чат;
    # лимит защищает LLM-бюджет и таблицу `session_chat_messages`.
    session_chat_max_turns: int = Field(default=100, ge=1)
    # Сколько чанков KB отдаёт тул `search_kb` в диалоге по сессии.
    session_chat_sources: int = Field(default=5, ge=0, le=20)
    # CC_ARTIFACTS_DIR: каталог шифрованных артефактов (AES-256-GCM, docs/18 §4.6;
    # в compose — volume, в тестах — tmp-каталог).
    artifacts_dir: str = "/var/lib/compass/artifacts"
    # CC_SIGNER_BACKEND: mock-ca — тестовый УЦ (PR P4). Ключ подписи — только в
    # демо-конфигурации и никогда не логируется.
    signer_backend: str = "mock-ca"
    # --- FR-40 (docs/18 §7): демо-PKI и подпись протокола ---------------------
    # CC_SIGNING_CERT_DAYS: срок жизни листового сертификата подписанта.
    # Отрицательное значение выпускает заведомо просроченный сертификат —
    # этим проверяется ветка `expired` в verify (демо/тесты, не прод).
    signing_cert_days: int = 365
    # CC_SIGNING_CA_CERT / CC_SIGNING_CA_KEY: PEM-файлы «внешнего» демо-УЦ.
    # Пусто → УЦ генерируется один раз и живёт в CC_ARTIFACTS_DIR/_ca/ (0600).
    signing_ca_cert: str | None = None
    signing_ca_key: str | None = None
    # Уполномоченное лицо МО — вторая подпись документа (п. 11 приказа 209н).
    # Данные демонстрационные: в проде их даёт кадровая выгрузка МО.
    signing_mo_fio: str = "Демидова Ольга Петровна"
    signing_mo_position: str = "Заместитель главного врача по медицинской части"
    signing_mo_org: str = "Демонстрационная медицинская организация «Компас»"
    signing_mo_snils: str | None = None
    # Должность врача-подписанта и его СНИЛС (СНИЛС пациента/врача в карточке
    # не хранится открытым текстом — пусто → поле не выводится, не выдумываем).
    signing_doctor_position: str = "Врач-психиатр"
    signing_doctor_snils: str | None = None
    # --- FR-41 (docs/18 §8): исходящая передача СЭМД в РЭМД/ЕГИСЗ ------------
    # CC_REMD_ENABLED: egress наружу. По умолчанию выключен (как CC_EMIAS_ENABLED):
    # клинический контур не должен молча отправлять документы в чужую ИС.
    remd_enabled: bool = False
    # CC_REMD_BASE_URL: REST-фасад РЭМД (в разработке — собственный мок remd-mock).
    remd_base_url: str = "http://127.0.0.1:8091"
    remd_client_id: str = "compass-lpu"  # CC_REMD_CLIENT_ID
    remd_client_secret: str = "remd-secret"  # CC_REMD_CLIENT_SECRET
    # CC_REMD_LPU_UID: идентификатор МО в ЕГИСЗ (в моке — фикстура).
    remd_lpu_uid: str = "1.2.643.5.1.13.13.12.2.77.1.1"
    # CC_REMD_CALLBACK_URL: адрес, на который РЭМД шлёт registerDocumentResult.
    remd_callback_url: str = ""
    # CC_REMD_CALLBACK_TOKEN: общий секрет callback-эндпоинта (X-Remd-Token).
    remd_callback_token: str = ""
    # CC_REMD_CALLBACK_MAX_KB: потолок тела callback'а (413 при превышении).
    # Конверт registerDocumentResult — сотни байт; лимит защищает память, а не разбор.
    remd_callback_max_kb: int = 256  # CC_REMD_CALLBACK_MAX_KB
    # CC_REMD_ALLOW_INSECURE: http допустим только для локального мока.
    remd_allow_insecure: bool = False
    remd_timeout: float = 10.0  # CC_REMD_TIMEOUT
    remd_max_attempts: int = 3  # CC_REMD_MAX_ATTEMPTS
    remd_max_response_bytes: int = 4 * 1024 * 1024  # CC_REMD_MAX_RESPONSE_BYTES
    # CC_REMD_RETRY_AFTER_CAP: потолок для Retry-After (шлюз может отдать 86400 с;
    # ожидание идёт внутри submit, поэтому потолок обязателен).
    remd_retry_after_cap: float = 60.0
    # CC_REMD_SENDING_STALE_SEC: через сколько `sending` без callback'а считается
    # потерянным и передача разрешается повторно.
    remd_sending_stale_sec: int = 900
    # Демонстрационные данные СЭМД: СНИЛС пациента и вид документа НСИ 1520.
    # СНИЛС пациента в карточке не хранится (как и СНИЛС врача, docs/18 §7.2),
    # поэтому для демо-контура он берётся из конфигурации; в живом контуре —
    # из связки ЕМИАС/ФРМР.
    remd_patient_snils: str = "112-233-445 95"  # CC_REMD_PATIENT_SNILS
    remd_document_kind: int = 34  # CC_REMD_DOCUMENT_KIND: код вида СЭМД НСИ 1520
    remd_org_name: str = "Демонстрационная медицинская организация «Компас»"
    remd_department_oid: str = "1.2.643.5.1.13.13.12.2.77.1.1.1"  # CC_REMD_DEPARTMENT_OID
    remd_department_name: str = "Психиатрическое отделение"  # CC_REMD_DEPARTMENT_NAME
    # CC_DEPERSONALIZE_NER: off | natasha | rubert | hybrid | remote (docs/12).
    # true/1 = natasha (обратная совместимость). rubert — redmadrobot-rnd/
    # rubert-base-pii-ner: Apache-2.0, FIO F1 0.933 (bench/ner); hybrid — rubert
    # + правила; remote — GPU-sidecar по HTTP (прод-путь, api без torch).
    depersonalize_ner: str = "off"
    ner_model: str = "redmadrobot-rnd/rubert-base-pii-ner"  # CC_NER_MODEL
    # CC_ML_URL: адрес ML-сайдкара (эмбеддинги, реранк, маскирование ПДн) —
    # GPU-сервис `ml` во внутренней сети compose (docs/08 §4). Пусто → сайдкар
    # не используется: остаются локальные профили эмбеддера и regex-путь ПДн.
    ml_url: str = ""
    ml_token: str = ""  # CC_ML_TOKEN: общий секрет api ↔ сайдкар (заголовок X-CC-ML-Token)
    # CC_ML_TIMEOUT: сек на один вызов сайдкара. Метки ПДн ждут этот таймаут в
    # запросе, поэтому он короче LLM-таймаута; при недоступном сайдкаре —
    # MlUnavailable (в прод-режиме egress ПДн fail-closed, docs/12).
    ml_timeout: float = Field(default=5.0, gt=0)
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
    # FR-48 (docs/03 §15): канал e-mail для кодов подтверждения. Пусто
    # CC_SMTP_HOST/CC_SMTP_FROM → канал не настроен: 503 всем адресам, кроме
    # явно разрешённого демо-режима (см. `mail_demo_codes`).
    smtp_host: str = ""  # CC_SMTP_HOST
    smtp_port: int = 587  # CC_SMTP_PORT
    smtp_user: str = ""  # CC_SMTP_USER
    smtp_password: str = ""  # CC_SMTP_PASSWORD — секрет, не логируется
    smtp_from: str = ""  # CC_SMTP_FROM: адрес отправителя
    smtp_tls: bool = True  # CC_SMTP_TLS: STARTTLS перед отправкой
    # CC_EMAIL_OTP: подтверждать вход пациента кодом на e-mail. false — вход
    # паролем как раньше; сотрудники всегда через TOTP.
    email_otp: bool = False
    # CC_MAIL_DEMO_CODES: отдавать код подтверждения прямо в ответе API, когда
    # SMTP не настроен. Только для локальных стендов и e2e: включённый флаг в
    # публичном контуре позволяет сбросить пароль чужого аккаунта, поэтому
    # одной настройки `CC_ALLOW_DEMO_SECRETS` для этого недостаточно.
    mail_demo_codes: bool = False
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

    @field_validator("rrf_k", mode="before")
    @classmethod
    def _rrf_k_unset(cls, value: object) -> object:
        """Пустое/неположительное CC_RRF_K = не задан → серверный FusionQuery(RRF).

        Пустую строку (CC_RRF_K=) pydantic сам в int не разберёт, а 0/минус по
        смыслу эквивалентны дефолту сервера — нормализуем в None, чтобы «пусто»
        в docs/08 не роняло старт.
        """
        raw = value.strip() if isinstance(value, str) else value
        if raw is None or raw == "":
            return None
        try:
            number = int(raw)  # type: ignore[arg-type] — env приходит строкой
        except (TypeError, ValueError):
            return value  # не число — пусть pydantic ругается сам
        return number if number > 0 else None

    @model_validator(mode="after")
    def _check_chunker(self) -> "Settings":
        """Нарезка КБ: молчаливая потеря текста запрещена (docs/08 §3).

        chunk_text скользит шагом max(size - overlap, 200): при size < 200 окно
        меньше шага и куски текста теряются, а size <= 0 даёт пустой корпус без
        ошибки. Ограничения общие для сида и enrichment (CC_KB_CHUNK_*).
        """
        if self.kb_chunk_chars < 200:
            raise ValueError(
                f"CC_KB_CHUNK_CHARS={self.kb_chunk_chars}: минимум 200 знаков — "
                "при меньшем размере chunk_text теряет текст (шаг окна >= 200)"
            )
        if not (0 <= self.kb_chunk_overlap < self.kb_chunk_chars):
            raise ValueError(
                f"CC_KB_CHUNK_OVERLAP={self.kb_chunk_overlap} должен быть в "
                f"[0, CC_KB_CHUNK_CHARS={self.kb_chunk_chars})"
            )
        return self

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
            # Fail-closed деперсонализации (docs/10, docs/12): как только настроен
            # LLM-egress, regex-пути недостаточно (свободные ФИО не в ростре
            # утекали). Прод обязан иметь ML-backend; недоступность модели на
            # старте/в запросе даст 503 DEPERSONALIZATION_UNAVAILABLE.
            if (
                self.assistant_llm_url.strip()
                or self.chat_insights_enabled
                # §4.3: аудио сессии уходит в ML-сайдкар и расшифровка хранится,
                # поэтому ИИ-блок сессии — такой же PHI-egress, как чат.
                or self.session_ai_enabled
            ):
                # Два допустимых прод-контура (docs/12): GPU-sidecar по HTTP
                # (remote + CC_ML_URL, рекомендуется) или локальный extra
                # (rubert/natasha/hybrid, torch внутри api-образа).
                backend = str(self.depersonalize_ner).strip().lower()
                if backend in ("", "off", "false", "0", "none", "no"):
                    raise ValueError(
                        "CC_PROD_MODE с egress в LLM/ML (чат, инсайты, ИИ-блок "
                        "сессии) требует ML-деперсонализацию: "
                        "CC_DEPERSONALIZE_NER=remote + CC_ML_URL (GPU-sidecar, "
                        "рекомендуется) либо hybrid/rubert/natasha + extra "
                        "api[ner-rubert]"
                    )
                if backend == "remote" and not self.ml_url.strip():
                    raise ValueError(
                        "CC_DEPERSONALIZE_NER=remote требует CC_ML_URL "
                        "(например http://ml:8080); альтернатива — локальный "
                        "hybrid/rubert/natasha + extra api[ner-rubert]"
                    )
        return self


settings = Settings()
