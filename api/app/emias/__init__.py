"""FR-25: интеграция с ЕМИАС (host-side клиент + мок внешнего контура).

Пакет:
  config  — снимок настроек (env CC_EMIAS_*) и проверка белого списка хостов
  auth    — кэш OAuth-токена (client_credentials, обновление на 80% TTL)
  client  — urllib-клиент FHIR/EMIAS: ретраи, кап тела, валидация Bundle
  mapping — чистые функции: FHIR-ресурс → поля локальных моделей
  sync    — идемпотентный upsert (emias_links/patient_medications/visits)
  mock    — самостоятельное FastAPI-приложение мока ЕМИАС (uvicorn app.emias.mock:app)
  cli     — python -m app.emias.cli sync-patients|sync-patient|link|health
"""
