# ML-сайдкар Clinical Compass

GPU-сервис с моделями, которые нельзя держать в образе `api` (torch и веса —
гигабайты, прод-api обязан оставаться torch-free):

| Эндпоинт | Модель | Назначение |
|---|---|---|
| `POST /v1/embeddings` | `Qwen/Qwen3-Embedding-0.6B` (профиль по умолчанию) или `intfloat/multilingual-e5-large-instruct` | dense-векторы БЗ, 1024; асимметричные префиксы query/passage |
| `POST /v1/rerank` | `BAAI/bge-reranker-v2-m3` (CrossEncoder) | cross-encoder реранк, сырые логиты |
| `POST /v1/mask` | `compass.depersonalize` + `redmadrobot-rnd/rubert-base-pii-ner` | маскирование ПДн; `model: hybrid\|regex`, `degraded: bool` |
| `POST /v1/transcribe` | `dropbox-dash/faster-whisper-large-v3-turbo` (faster-whisper, CT2) + диаризация sherpa-onnx | расшифровка аудио сессии на русском; `diarized/degraded: bool`, реплики с метками спикеров `A`/`B` |
| `POST /v1/diarize` | sherpa-onnx (тот же диаризатор) | метки `A`/`B` для уже готовых сегментов ASR: второй этап расшифровки, текст врач получает раньше |
| `GET /health` | — | `status` (`loading`/`ready`/`error`), `device`, `models`, `error: bool` |

Контракт заморожен в `api/app/services/ml_client.py`; конфигурация и
обоснование — `docs/08 §4.1`, маскирование — `docs/12`, граница доверия —
`docs/10 §3.1`. Транскрипция выключена по умолчанию (`CC_ML_ASR=0`) — раздел
«Транскрипция» ниже.

## Установка и запуск

```bash
# из корня репозитория
uv pip install -r ml/requirements.txt        # + torch cu128 отдельно:
# pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0
PYTHONPATH=ml:core CC_ML_DEVICE=cuda CC_ML_TOKEN=dev \
  uvicorn app:app --host 0.0.0.0 --port 8080
```

`CC_ML_TOKEN` обязателен: без него приложение не стартует (fail-closed).
Для локальной отладки без токена есть явный `CC_ML_ALLOW_ANONYMOUS=1`.

Контейнер (профиль `gpu`, видеокарта через `deploy.resources.reservations`):

```bash
tools/gen-secrets.sh infra/.env                        # создаёт CC_ML_TOKEN
docker compose -f infra/docker-compose.yml --profile gpu run --rm ml-warmup
docker compose -f infra/docker-compose.yml --profile gpu up -d ml
docker compose -f infra/docker-compose.yml --profile gpu logs -f ml
docker compose -f infra/docker-compose.yml --profile gpu exec ml python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/health')))"
```

Порт на хост не публикуется: `ml` доступен только из сети compose
(`http://ml:8080` для `api`). Сеть `mlnet` объявлена `internal: true` — у
`ml` нет egress и он не видит postgres/qdrant/redis; api подключён к `mlnet`
вторым интерфейсом. Веса (~4 ГБ, +1.6 ГБ при `CC_ML_ASR=1`) скачивает
одноразовый `ml-warmup` (default-сеть, единственный шаг с egress), после чего
`ml` стартует с `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`. Пустой том при
offline-старте — ошибка загрузки.

Транскрипцию включает `CC_ML_ASR=1` в `infra/.env` (плюс повторный
`ml-warmup`, чтобы в том попали веса Whisper). Так как `av` несёт собственные
FFmpeg-библиотеки, отдельный `ffmpeg` в образ ставить не нужно.

## Переменные окружения

| Переменная | Дефолт | Смысл |
|---|---|---|
| `CC_ML_STUB` | `0` | `1` — детерминированные заглушки без torch (тесты/CI) |
| `CC_ML_DEVICE` | `auto` | `auto` \| `cuda` \| `cpu`; `auto` — CUDA при доступной карте |
| `CC_ML_TOKEN` | — | общий секрет `X-CC-ML-Token`; **обязателен**, иначе старт падает |
| `CC_ML_ALLOW_ANONYMOUS` | `0` | `1` — разрешить пустой токен (только локальная отладка) |
| `CC_ML_DEFAULT_PROFILE` | `qwen3-0.6b` | профиль эмбеддингов по умолчанию (`qwen3-0.6b`, `e5-large-instruct`, `bge-m3`) |
| `CC_ML_PROFILES` | — | JSON `{имя: {model, dense_size, query_prefix, passage_prefix}}` |
| `CC_ML_QUERY_INSTRUCTION` | англ. инструкция | текст `Instruct:` для instruct-эмбеддеров |
| `CC_ML_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | cross-encoder |
| `CC_ML_NER_MODEL` | `redmadrobot-rnd/rubert-base-pii-ner` | token-classifier для ФИО/адресов |
| `CC_ML_ASR` | `0` | `1` — включить `POST /v1/transcribe` (иначе 503 `asr_disabled`) |
| `CC_ML_ASR_MODEL` | `dropbox-dash/faster-whisper-large-v3-turbo` | HF-id CT2-весов Whisper |
| `CC_ML_ASR_DEVICE` | значение `CC_ML_DEVICE` | `cuda` \| `cpu` — ASR можно увести на CPU отдельно |
| `CC_ML_ASR_COMPUTE` | `int8_float16` на CUDA, `int8` на CPU | тип вычислений CTranslate2 |
| `CC_ML_ASR_MAX_MB` | `200` | потолок аудио в МБ (413 `payload_too_large`) |
| `CC_ML_ASR_CONCURRENCY` | `1` (макс. 4) | сколько распознаваний одновременно; остальные получают 503 `busy` + `Retry-After: 5` |
| `HF_HOME` | `/models` | кеш весов (том `ml-models`) |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `1` в compose | запрет сетевых обращений к HF |

Пример `CC_ML_PROFILES`:

```json
{"qwen3-0.6b": {"model": "Qwen/Qwen3-Embedding-0.6B", "dense_size": 1024},
 "custom": {"model": "org/model", "dense_size": 768, "query_prefix": "", "passage_prefix": ""}}
```

## Ограничения входа

Пороги — в `ml/models.py` (`MAX_TEXTS=64`, `MAX_DOCUMENTS=256`,
`MAX_CHARS=20000`, `MAX_NAMES=256`); превышение — 422 `invalid_request`.
Дополнительно тело запроса отсекается по `Content-Length` до парсинга JSON:
mask — 1 МиБ, embeddings — 6 МиБ, rerank — 24 МиБ, transcribe —
`CC_ML_ASR_MAX_MB` + 64 КиБ мультипарт-обвязки (413 `payload_too_large`).

Длинный текст для NER режется на окна по 1200 символов с перекрытием 200
(`NER_CHUNK_CHARS`/`NER_CHUNK_OVERLAP`), поэтому ФИО в конце документа
находится, а не отбрасывается 512-токенным лимитом rubert. При сбое NER
ответ честный: `model="regex"`, `degraded=true` (api в prod считает это
отказом backend'а, docs/12).

## Транскрипция (`POST /v1/transcribe`)

Расшифровка аудио сессии. Контракт api-стороны — §4.3 документа «Клинические
сессии» (ветка `feat/sessions-core`; в `main` этого файла ещё нет, поэтому
поля ниже — источник истины для обоих концов).

Включение — `CC_ML_ASR=1`; по умолчанию эндпоинт отвечает 503 `asr_disabled`,
чтобы веса Whisper не занимали VRAM у стека, которому транскрипция не нужна.

Запрос — multipart: `file` (обязателен), `language` (по умолчанию `ru`;
принимаются только коды Whisper из `ASR_LANGUAGES` в `ml/models.py`, иначе
400 — значение проверяется до бэкенда), `diarize` (`false`). Типы —
`audio/webm|ogg|wav|mpeg|mp4|m4a` и `video/webm|mp4` (`;codecs=opus`
допускается), иначе 415 `unsupported_media`. Последние два — те же
контейнеры: MediaRecorder с видеодорожкой и любой прокси, определяющий тип по
расширению (`session.webm` → `video/webm`), иначе получали бы 415 на живую
запись; декодирование всё равно по содержимому, берётся аудиодорожка. Размер — до
`CC_ML_ASR_MAX_MB` (200 МБ ≈ 3 ч opus) и проверяется дважды: по
`Content-Length` до разбора мультипарта и по фактической длине файла.

```bash
curl -s -X POST http://ml:8080/v1/transcribe \
  -H "X-CC-ML-Token: $CC_ML_TOKEN" \
  -F file=@session.webm -F language=ru | jq
```

```json
{
  "model": "dropbox-dash/faster-whisper-large-v3-turbo",
  "device": "cuda",
  "duration_s": 138.356,
  "rtf": 0.0321,
  "language": "ru",
  "segments": [
    {"start": 0.0, "end": 4.14, "speaker": "A",
     "text": "Пациент жалуется на боль в груди и одышку в течение трёх дней."}
  ],
  "speakers": [
    {"id": "A", "label": null, "seconds": 74.2, "segments": 12},
    {"id": "B", "label": null, "seconds": 61.5, "segments": 9}
  ],
  "diarized": true,
  "degraded": false
}
```

Гарантии ответа: `start < end`, сегменты отсортированы по `start` и не
пересекаются, `duration_s` — длительность из декодера,
`rtf = время_обработки / duration_s` (ASR + диаризация). `label` у спикера
всегда `null`: роль «врач/пациент» назначает api, а не сайдкар (метки `A`/`B` —
кластеры, причём `A` — тот, кто заговорил первым).

### Диаризация (`diarize=true`, FR-42)

Бэкенд выбирается `CC_ML_DIARIZE_BACKEND` — обе реализации живут за одним
интерфейсом `diarize(audio, duration_s) -> [{start, end, speaker}]`, метки
кластеров нумеруются одинаково, поэтому маппинг на сегменты и метки `A`/`B`
от бэкенда не зависят.

| Бэкенд | Лицензия и вес | Замер на 50.6 с русского диалога | Вывод |
|---|---|---|---|
| `sherpa` (дефолт) | MIT + Apache-2.0, веса 33 МБ, `sherpa-onnx` 2.0 (CPU) | 12/12 реплик верно, RTF 0.08–0.12 при load ≈6/16, на загруженном хосте до 0.16 (2 потока CPU), VRAM 0, RSS ~1.3 ГБ | дефолт: встаёт из PyPI, веса с GitHub k2-fsa, работает без GPU |
| `sortformer` | NVIDIA Open Model License (коммерческое использование разрешено), `nvidia/diar_streaming_sortformer_4spk-v2.1`; `nemo_toolkit[asr]` тянет свой torch 2.14.1+cu130, venv 6.3 ГБ | 12/12 реплик верно, RTF 0.33 (GPU), 918 МБ VRAM, RAM-пик 2.5 ГБ на CPU, до 4 спикеров | проверен вживую, но в образ ml не входит: конфликтует с пином torch 2.11, нужен отдельный образ |

Почему не pyannote.audio и не «SOTA»-модели: `pyannote/speaker-diarization-community-1`
и `pyannote/segmentation-3.0` на HF **gated** (CC-BY-4.0 / MIT, но нужно ручное
принятие условий, веса не перераздаваемы) — поэтому берём их ONNX-порт от k2-fsa,
который раздаётся свободно. `BUT-FIT/diarizen-wavlm-large-s80-md` и
`nvidia/diar_sortformer_4spk-v1` — **CC-BY-NC-4.0**, некоммерческие, исключены.

Если выбранный бэкенд не поднялся (нет `nemo_toolkit`, нет весов sherpa, опечатка
в имени бэкенда), ответ остаётся 200 с `diarized=false`, `degraded=true`, а в лог
один раз уходит код **`diarize_backend_unavailable`** с причиной.


### sherpa-onnx (дефолт)

| Что | Значение |
|---|---|
| Библиотека | `sherpa-onnx` 2.0 (pip-колесо, свой onnxruntime внутри, **CPU**; GPU-сборки в PyPI нет и не нужна) |
| Segmentation | pyannote segmentation 3.0 ONNX, **MIT**, `model.onnx` 5.7 МБ (int8 — 1.5 МБ, но на тесте схлопывал два голоса в один кластер: 6/12 реплик) |
| Embeddings | 3D-Speaker CAM++ zh/en advanced ONNX, **Apache-2.0**, 27 МБ |
| Кластеризация | AHC внутри sherpa, `num_clusters=2` (врач + пациент) |
| Источник весов | релизы k2-fsa на GitHub (не HF: pyannote.audio там gated), sha256 в `ml/models.py`, качает `ml-warmup` в `/models/diarize` |
| Замер | 50.6 с синтетического русского диалога (2 голоса piper, 12 реплик): **12/12** реплик размечены верно, RTF **0.08–0.12 при load ≈6/16** (свои прогоны 0.100 / 0.117 / 0.121), на загруженном хосте до 0.16 (независимый замер 0.116 / 0.162); VRAM 0, RSS процесса ~1.3 ГБ вместе с весами |

Аудио берётся из того же PyAV-декодера, что у Whisper (16 кГц mono float32), —
повторного декодирования нет. Сшивка с сегментами ASR — по максимальному
перекрытию по времени.

Ограничения (сознательные): ровно два участника; пересекающаяся речь уходит
говорящему с большим перекрытием; реплики короче `min_duration_on=0.3` с
отбрасываются; соло-запись тоже делится на два кластера (поэтому api помечает
роли как допущение); схожие голоса и шум кабинета снижают точность — роли
подтверждает врач.

Деградация: нет весов / `CC_ML_DIARIZE=0` / сбой модели — ответ **200** с
`diarized=false`, `speaker: null`, `degraded=true`; причина пишется в лог один
раз (статус липкий до рестарта). Диаризация никогда не роняет ASR: `diarize=true`
не меняет коды ошибок транскрипции.

Коды ошибок: 401/403 — токен; 415 `unsupported_media`; 413
`payload_too_large`; 400 `invalid_request` (пустой файл, код языка не из
списка); 422 `asr_failed` (аудио не декодируется/не распознаётся — сбой
бэкенда на самом файле, а не 500); 503 `asr_disabled` (`CC_ML_ASR=0` — без
`Retry-After`, повтор не поможет); 503 `model_loading` + `Retry-After: 10`
(веса грузятся фоном после `ready`, ответ отдаётся сразу — повторить через
10 с); 503 `model_unavailable` (загрузка упала, подробности в
логах); 503 `busy` + `Retry-After: 5` (все слоты распознавания заняты,
`CC_ML_ASR_CONCURRENCY`, по умолчанию 1 — второй параллельный запрос честно
получает отказ, а не висит в очереди).

### Бэкенд и замеры

faster-whisper 1.2 / CTranslate2 4.8, `large-v3-turbo` в int8: декодирование
через PyAV (внешний ffmpeg в образе не нужен), `vad_filter=True` (silero
выбрасывает тишину и режет запись на 30-секундные окна), `beam_size=5`,
`language="ru"`, без word-timestamps. Модель грузится лениво фоном и висит в
VRAM до рестарта (выгружать её на каждый запрос дороже, чем держать).

Замер на RTX 5090 Laptop 24 ГБ, 138 с русской речи (синтез piper), int8_float16:

| Режим | Загрузка весов | VRAM процесса | 138 с аудио | RTF |
|---|---|---|---|---|
| CUDA int8_float16, beam 5 | 4.7–5.6 с (локальный кеш) | +1.3 ГБ к простою | 3.0–5.3 с | **0.022–0.038** |
| CUDA, первый прогон после загрузки (холодные ядра) | — | — | 6.5 с | 0.047 |
| CPU int8, beam 5, 8 потоков | — | 0 (RAM) | 19.7 с на 30 с | **0.66** |

То есть 20-минутная сессия — ≈ 30 с на GPU и ≈ 13 мин на CPU: CPU-путь
годится только для асинхронного фона. Пик VRAM ASR ≈ 1.5 ГБ, суммарно с
тремя остальными моделями — в пределах 6 ГБ.

Разброс 0.022–0.038 — это разные прогоны на одной карте (фоновая нагрузка
ноутбука, прогрев ядер); для 20-минутной сессии это всё равно 30–50 с.

### Лицензии кандидатов

| Кандидат | RU WER | Лицензия | Вердикт |
|---|---|---|---|
| GigaAM-v3 `e2e_rnnt` (`ai-sage/GigaAM-v3`) | **8.3%** (3.9% farfield) | MIT | не поставлен, см. ниже |
| Whisper large-v3-turbo → CT2 `dropbox-dash/faster-whisper-large-v3-turbo` | ≈21% (harness GigaAM), ≈16% farfield | MIT | **выбран** |
| T-one (`t-tech/T-one`) | 16.3% с LM | Apache-2.0 | телефонный домен, без пунктуации |
| parakeet-tdt-0.6b-v3 | 5.5% FLEURS-ru | CC-BY-4.0 | нужна атрибуция, кандидат «на 2-е мнение» |
| Vosk RU | 19.5–36% | Apache-2.0 | CPU-путь, качество ниже |
| Silero STT | — | **CC BY-NC 4.0** | дисквалифицирован (некоммерческая) |

Зависимости пути — faster-whisper, CTranslate2, PyAV, onnxruntime — MIT;
копилефта нет. `av` пинится `<19`: PyAV 19 убрал аргумент `metadata_errors`,
на котором падает `faster_whisper.audio.decode_audio`.

### Почему не GigaAM (апгрейд-путь)

GigaAM-v3 точнее на русском в 2.5 раза, но (1) `transcribe` жёстко режет вход
на 25 с — нужен свой VAD-чанкинг с абсолютными офсетами и склейкой границ;
(2) API `gigaam`/`onnx-asr` молодые: сегменты и VAD придётся собирать самим;
(3) выигрыш в WER не подтверждён на нашем домене — 8.3% получены на
Golos/Natural Speech/Callcenter, а не на клиническом диалоге.

Гейт для перехода (нужны все три пункта):

1. in-domain WER на 20–30 размеченных клинических записях: GigaAM ≤ Whisper − 30%;
2. RTF ≤ 0.05 на целевой карте вместе с VAD-чанкингом;
3. стабильный релиз `gigaam`/`onnx-asr` с сегментами и VAD, совместимый с MIT.

До этого в проде один бэкенд: два полурабочих ASR дороже, чем один честный.

## Диаризация готовой расшифровки (`POST /v1/diarize`)

Второй этап расшифровки (docs/18 §4.3). api сначала зовёт `/v1/transcribe`
с `diarize=false`, сохраняет текст и отдаёт его врачу, а затем просит только
метки — поэтому ожидание врача равно времени ASR, а не ASR + диаризация
(замер 2026-10-27: RTF ASR 0.08, диаризации 0.10–0.18).

Запрос — multipart: `file` (то же аудио, что ушло в `/v1/transcribe`),
`segments` — JSON-массив `[{start, end, …}]` (текст не нужен, лишние поля
игнорируются), `duration_s` — длительность из ответа ASR. Пределы:
`CC_ML_ASR_MAX_MB` для файла и `MAX_SEGMENTS = 8000` для массива, иначе 413.

```bash
curl -s -X POST http://ml:8080/v1/diarize \
  -H "X-CC-ML-Token: $CC_ML_TOKEN" \
  -F file=@session.webm \
  -F 'segments=[{"start":0,"end":5.2},{"start":5.2,"end":9.8}]' \
  -F duration_s=9.8 | jq '.diarized, .segments[].speaker'
```

Ответ: `{segments (с `speaker`), speakers [{id, label, seconds, segments}],
diarized, degraded, reason}`. Текст сегментов не переписывается — api хранит
уже деперсонализированный текст и берёт отсюда только метки. Ошибки: 400
`invalid_request` (не JSON, не массив, нет `start`/`end`, `start >= end`), 413
`payload_too_large`, 415 `unsupported_media`, 503 `busy` (занят слот ASR/диаризации).

## Модели и память

Замер на RTX 5090 Laptop 24 ГБ (bf16, три модели поиска/маскирования
загружены): VRAM +4.1 ГБ, холодная загрузка из локального HF-кеша ≈ 20 с (в
контейнере после `ml-warmup` — без скачивания). Транскрипция добавляет ещё
≈1.3 ГБ (см. «Транскрипция»). Латентности и точная таблица —
`REPORT-ml-sidecar.md` в корне репозитория.

## Тесты

```bash
python -m pytest ml/tests -q            # stub, без torch/GPU/сети/токена
```

Проверяются формы контракта, обязательный токен и анонимный режим только по
флагу, 503 во время загрузки, лимиты и отсечение тела, порядок и границы
спанов, выравнивание реранка, размерность векторов, окна NER и деградация
`degraded=true`.

Отдельно — контракт `/v1/transcribe` (`tests/test_transcribe.py`): форма
ответа, 401 без токена, 413 (и middleware, и точная проверка файла), 415 на
чужой тип и 200 на `video/webm|mp4`, конверт `{"detail":{"code","message"}}`
для 400 самого python-multipart (часть > 1 МиБ, multipart без boundary), 400
на пустой файл и на код языка не из `ASR_LANGUAGES`, 422 `asr_failed` при
сбое бэкенда, 503 `asr_disabled` (без `Retry-After`), 503 `model_loading` с
`Retry-After` и без ожидания замка загрузчика, 503 `busy` для второго
параллельного запроса, кривой `CC_ML_ASR_MAX_MB`/`CC_ML_ASR_CONCURRENCY` не
роняет `create_app`, монотонность и непересечение сегментов, `degraded`
только от `diarize=true`.

## Диагностика

| Симптом | Причина/что делать |
|---|---|
| старт падает `CC_ML_TOKEN не задан` | сгенерируйте `.env` (`tools/gen-secrets.sh`) или задайте токен |
| `RuntimeError: ... не загрузились` / `model_unavailable` | смотрите `logs -f ml`: нет весов (запустите `ml-warmup`) или OOM |
| `CUDA out of memory` | освободить VRAM (другие LLM на карте) или `CC_ML_DEVICE=cpu` |
| `ml` не видит интернет | так и задумано (`mlnet` внутренняя); качайте веса через `ml-warmup` |
| 401/403 от api | `CC_ML_TOKEN` не совпадает у `api` и `ml` |
| `device: cpu` при `CC_ML_DEVICE=cuda` | нет `nvidia-container-toolkit`/драйвера — проверьте `docker run --gpus all` |
| 503 `asr_disabled` | транскрипция выключена: `CC_ML_ASR=1` в `.env` и перезапуск `ml` |
| 503 `model_loading` дольше минуты | веса ASR не в томе: `ml-warmup` с `CC_ML_ASR=1` |
| `libcublas.so.12 is not found` | CTranslate2 без `LD_LIBRARY_PATH` на nvidia-колёса torch (в образе задан) |
| `TypeError: open() got an unexpected keyword argument 'metadata_errors'` | PyAV 19: нужен `av<19` (пин в `requirements.txt`) |
