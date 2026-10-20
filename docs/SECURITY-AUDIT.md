# Clinical Compass — Security & RF-Compliance Audit Report

**Date:** 2026-10-19
**Scope reviewed:** `api/app/` (routers, services, models, security, db, config, seed, errors, deps, main, ratelimit), `api/alembic/versions/` (all migrations), `infra/docker-compose.yml`, `ui/nginx.conf`, `ui/src/api.ts`, `mobile/capacitor.config.ts`, `docs/02`, `docs/03`, `docs/05`, `docs/09`. Read-only review; no payloads executed, no network calls.

**Verified clean (no finding):** RBAC + `clinician_roster` enforcement is consistent — every patient-scoped clinician function (`prescribe_scales`, `report`, `patient_context`, `create_adverse_event`, `list_adverse_events`, `adherence_summary`) calls `_require_roster` (`services/clinician.py:22-25`); patient-facing endpoints derive `patient_id` from the JWT (`user.id`), not from a path param, so **no IDOR** was found. `require_consent` gates all patient writes/reads of special-category data. Argon2 passwords, JWT 15min/7d TTL, TOTP 2FA, audit append-only trigger, login rate limiter — accepted as known and not re-reported.

---

## F1 — CRITICAL: Special-category PDn stored unencrypted at rest (AES-256/KMS claim unimplemented)

**Evidence.** `docs/02-системный-анализ.md:92-102` promises `AES-256 (БД)` for *every* special-category dataset (account, profile, consent, scale answers/scores/triage, escalations, diary, chat, audit, voice transcripts), and `docs/02:140` lists «шифрование … данных (AES-256)» as a required ФСТЭК №21 measure. The actual schema stores all of it in **plaintext columns**:
- `api/app/models.py` — `Answer.value: int`, `Assessment` (score/triage as plain columns), `DiaryEntry.content: String` + `tags: JSONB`, `ChatMessage.content: String`, `AdverseEvent.event_text: Text`, `Escalation`, `Adherence`.
- `api/alembic/versions/327608462007_initial_schema.py` — DDL creates plain `VARCHAR`/`TEXT`/`JSONB`/`INTEGER` columns; **no `pgcrypto` extension, no `bytea`/encrypted columns, no TDE**.
- `infra/docker-compose.yml` — `postgres:16-alpine` on a bare `pgdata` Docker volume with **no encrypted filesystem**; `POSTGRES_PASSWORD: compass`.
- No KMS integration anywhere — `api/app/config.py` has no KMS/key fields; grep for `KMS|HSM|encrypt|шифрован` in `api/app/` returns nothing.

**Compliance.** 152-ФЗ art.10 (special-category medical PDn); ПП РФ №1119 → ISPDn category 1 / threat type 2 ⇒ required защищённость **УЗ-2** (protection of PDn at rest against unauthorized access); ФСТЭК приказ №21 (шифрование данных — listed as required in the project's own `docs/02:140`); ГОСТ Р 56939-2016 (confidentiality control).

**Fix.** Encrypt special-category columns at rest: enable `pgcrypto` and store `answers.value`/`diary.content`/`chat_messages.content`/`adverse_events.event_text`/`escalations` as `bytea` encrypted with `pgp_sym_encrypt` using a key from a real KMS (Yandex KMS / VK Cloud KMS) or at minimum an LUKS-encrypted volume + DB-level TDE. Back the docs/02 «AES-256» claim with the actual mechanism; until then remove the claim (ГОСТ 56939 requires documented controls to match reality).

---

## F2 — HIGH: Consent revocation does not destroy data (152-ФЗ art.9 ч.2-3 ≤30 days)

**Evidence.** `api/app/routers/consent.py` `/revoke` only flips a flag:
```python
@router.post("/revoke", status_code=204)
def revoke(user, db):
    consent = _active(db, user.id)
    ...
    consent.revoked_at = datetime.now(timezone.utc)   # PDn left in place
    db.commit()
```
`require_consent` (`deps.py:52-57`) blocks *new* operations post-revocation, but all historical PDn (answers, assessments, diary, chat, adverse_events, escalations) remains indefinitely. Grep across `api/app/` for deletion/retention/scheduling (`delete(|уничтож|retention|destroy|APScheduler|BackgroundTask`) confirms **no destruction path and no scheduler** — the only `delete()` calls are `delete(Drug)` (catalog, `seed.py:78`) and `delete_collection` (Qdrant KB, `kb_seed.py:225`), none touching PDn. The project's own `docs/02:138` states the requirement: «при отзыве — прекращение обработки и уничтожение спецкатегорий ≤30 дн., ст. 9 ч. 2–3».

**Compliance.** 152-ФЗ art.9 ч.2-3 (consent revocation ⇒ cease processing and destroy special-category PDn within 30 days); art.21 (destruction upon achieving processing purpose).

**Fix.** On `/revoke`, enqueue a 30-day destruction job (or immediate anonymization) that deletes/irreversibly anonymizes the user's `answers`, `assessments`, `diary_entries`, `chat_messages`, `adverse_events`, `escalations`, `runs` (retain only `consents` row + `audit_log` per art.9 ч.4 "5 лет после отзыва" and medical-record retention). Add a scheduled worker (APScheduler/Celery beat) and a `destruction_scheduled_at` column.

---

## F3 — HIGH: Hardcoded/committed production-shape secrets (JWT forgery → auth bypass)

**Evidence.** `infra/docker-compose.yml`:
```yaml
POSTGRES_PASSWORD: compass
CC_DATABASE_URL: postgresql+psycopg://compass:compass@postgres:5432/compass
CC_JWT_SECRET: demo-jwt-secret-0123456789abcdefghij
CC_VAPID_PRIVATE_KEY: LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0t...  # base64 PKCS8 PEM, committed
```
`api/app/config.py` defaults: `jwt_secret: str = "dev-secret-change-me-0123456789abcdef"`, `database_url = "...compass:compass@..."`. `docs/03:122` *claims* «продакшн-ключи — .env/env/KMS (вне git)» but **no KMS code exists** and the weak defaults ship in source. Anyone with repo read access can forge a valid HS256 JWT for any `sub`/role (access *and* refresh tokens share the same secret, `security.py:57,70`) → full authentication bypass as ADMIN/CLINICIAN → unrestricted special-category PDn access.

**Compliance.** ФСТЭК №21 (ИАФ — аутентификация/управление доступом; защита аутентификационной информации); ГОСТ Р 56939-2016 (secure configuration, secret management).

**Fix.** Remove all secrets from compose and source; load from a real secret store (KMS/HashiCorp Vault/encrypted `.env` outside git). Fail-fast at startup if `CC_JWT_SECRET` is missing or below entropy threshold (reject the current defaults). Rotate the committed VAPID key and DB password. Use asymmetric JWT (RS256) with key rotation, or at minimum a high-entropy per-deployment secret.

---

## F4 — HIGH: Refresh tokens are stateless JWTs with no rotation and no revocation

**Evidence.** `api/app/security.py:62-70` — `create_refresh_token` embeds only `sub/type/iat/exp`; **no `jti`, no server-side record**:
```python
def create_refresh_token(user_id):
    payload = {"sub": str(user_id), "type": "refresh", "iat": now,
               "exp": now + timedelta(days=settings.refresh_ttl_days)}  # 7 days
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
```
`api/app/routers/auth.py:80-91` `/refresh` issues a new access token **without rotating or invalidating** the refresh token, and there is **no `/logout` or revocation endpoint** (auth router has only `register`/`login`/`refresh`). No denylist/jti store exists. A stolen refresh token therefore remains valid for up to **7 days** with no way to invalidate it — even after the user "logs out" (the UI only clears `localStorage`).

**Compliance.** ФСТЭК №21 (ИАФ — управление сеансами/сессиями; отзыв учетных данных); ГОСТ Р 56939-2016.

**Fix.** Store refresh tokens server-side (hashed) in a `refresh_tokens` table with a `revoked` flag and `rotated_from` chain; on `/refresh`, rotate (issue new + revoke old) and detect reuse → revoke family. Add `/auth/logout` to revoke the active refresh token. Consider shortening refresh TTL.

---

## F5 — MEDIUM: 422 validation bodies echo submitted input (passwords + special-category content)

**Evidence.** `api/app/errors.py`:
```python
async def validation_handler(request, exc: RequestValidationError):
    return JSONResponse(422, _problem(422, "VALIDATION", str(exc.errors()[:3]), ...))
```
Pydantic v2 `ValidationError.errors()` includes the **`input`** field (the offending value) by default; the handler serializes it verbatim. Concretely:
- `RegisterBody.password = Field(min_length=8, max_length=128)` (`auth.py:24`) → a too-short/too-long password is echoed in the 422 body (credential disclosure).
- `DiaryBody.content = Field(max_length=4000)` and `ChatBody.content = Field(max_length=2000)` → a too-long **special-category** diary/chat submission has its PHI content echoed back in the 422 body (and into any response/Proxy log that captures it).

**Compliance.** 152-ФЗ (минимализация; недопустимость раскрытия ПДн); ФСТЭК №21 (аутентификационная информация не должна раскрываться).

**Fix.** Call `exc.errors(include_input=False)` (or strip `input`/`url` keys) before serializing; return only `loc`, `msg`, `type`. Never echo credential or PHI fields.

---

## F6 — MEDIUM: Audit trail is incomplete and IP-unreliable (claims vs. reality)

**Evidence.**
- `api/app/services/audit.py:10-11` — `ip` is optional and **not passed** by most callers: `clinician.py` audit calls (`PRESCRIBE_SCALE:62`, `READ_REPORT:244`, `ASK_ASSISTANT_WITH_PATIENT:263`, `ADVERSE_EVENT:290`) omit `ip` ⇒ stored as `NULL`. Only `assistant.py:67-69` and `auth.py:60` pass `ip`.
- Where `ip` *is* captured it uses `request.client.host` (`auth.py:60`, `assistant.py:67`). Behind nginx this is the **nginx container IP**, not the real client — `ui/nginx.conf` sets `X-Real-IP $remote_addr` but FastAPI has **no `ProxyHeaders`/`TrustedHost` middleware** (grep for `X-Forwarded|X-Real-IP|ProxyHeaders|TrustedHost` in `api/app/` = none). So even captured IPs are wrong.
- `docs/03:114` claims «Все обращения к спецкатегориям пишутся в audit_log, включая чтения по roster» — but patient **self-reads** of special-category data are **not** audited: `GET /diary` (`diary.py`), `GET /chat/thread` (`chat.py`), `GET /screenings`/`GET /me/prescribed-scales` (`screenings.py`) call no `audit.record`.

**Compliance.** ФСТЭК №21 (журналирование действий субъектов доступа — must capture actor, action, object, and source address reliably); ГОСТ Р 56939-2016 (audit trail integrity); 152-ФЗ (accountability for special-category processing).

**Fix.** Add `TrustedHostMiddleware` + `X-Forwarded-For` trusted-proxy parsing (or `uvicorn --proxy-headers`) so `request.client` reflects the real origin; pass `ip` from a `Request` dependency into every `audit.record` call; audit patient self-reads of special-category endpoints; document the audit schema.

---

## F7 — MEDIUM: No backup/recovery mechanism (ФСТЭК резервирование)

**Evidence.** `infra/docker-compose.yml` defines only live data volumes (`pgdata`, `qdrant-data`) — **no backup service, no `pg_dump` cron, no restore path**. `docs/02:140` lists «резервирование» as a required ФСТЭК №21 measure. The team self-acknowledges the gap in `docs/09:113` (P10): «Отказоустойчивость/backup не демонстрированы … pg_dump cron в docs/infra, restore не проверен» — yet no such cron exists in the repo.

**Compliance.** ФСТЭК №21 (резервирование/восстановление); ПП РФ №1119 (УЗ-2 — обеспечение восстановления).

**Fix.** Implement scheduled encrypted backups (pg_dump → encrypted object storage, RF region), document and **test** restore (RPO/RTO), and retain per 152-ФЗ/medical-record retention. Backups must themselves be encrypted (currently F1 leaves plaintext PDn, so backups would inherit that).

---

## F8 — MEDIUM: Python dependencies not pinned (no lockfile)

**Evidence.** `api/pyproject.toml` uses open lower bounds (`fastapi>=0.115`, `sqlalchemy>=2.0.30`, `pyjwt>=2.8`, `argon2-cffi>=23.1`, `pyotp>=2.9`, `qdrant-client>=1.10`, `langgraph>=0.2`, `psycopg[binary]>=3.2`, `pydantic>=2.7` …). Glob for `poetry.lock|uv.lock|requirements*.txt|constraints*.txt` in `api/` returns **nothing** (the UI has `package-lock.json`; the API does not). Builds are non-reproducible and can pull in transitively-vulnerable versions without notice.

**Compliance.** ГОСТ Р 56939-2016 (управление зависимостями/уязвимостями, воспроизводимость); ГОСТ Р ИСО/МЭК 12207-2010 (конфигурационное управление).

**Fix.** Generate and commit a lockfile (`uv lock` / `pip-compile` → `requirements.lock`) with hashes; pin all transitive deps; run `pip-audit`/Dependabot in CI (`.github/workflows/ci.yml`).

---

## F9 — MEDIUM: Access + refresh tokens stored in localStorage (XSS exfiltration)

**Evidence.** `ui/src/api.ts`:
```ts
export function getToken() { return localStorage.getItem("cc_token"); }
export function getRefreshToken() { return localStorage.getItem("cc_refresh"); }
```
Both the 15-min access token and the 7-day refresh token (F4) live in `localStorage`, readable by any injected script (XSS). For a special-category medical system this expands the blast radius of an XSS to full session takeover + 7-day persistence.

**Compliance.** ФСТЭК №21 (ИАФ — защита аутентификационных токенов); ГОСТ Р 56939-2016.

**Fix.** Move tokens to `HttpOnly; Secure; SameSite=Strict` cookies (CSRF-protected); keep nothing sensitive in `localStorage`. This pairs with F4 (revocable server-side refresh).

---

## F10 — LOW: `seed.py` f-string SQL for `clinician_roster` (secure-dev anti-pattern, not API-reachable)

**Evidence.** `api/app/seed.py:104-106`:
```python
s.execute(text(f"INSERT INTO clinician_roster(clinician_id, patient_id) "
               f"VALUES ('{doc_id}', '{pid}') ON CONFLICT DO NOTHING"))
```
Reachability check: `doc_id`/`pid` are **internally-generated UUIDs** (`patient.id`, the seeded clinician's id), and `seed.py` runs only via `python -m app.seed --reset` (no router/endpoint invokes it). So this is **not** injectable through the API. It remains a ГОСТ 56939 code-quality/secure-dev defect (string-built SQL).

**Compliance.** ГОСТ Р 56939-2016 (secure coding — parameterized queries). No other raw SQL with user input exists: grep found only `db.py` audit-trigger DDL (static) and `TRUNCATE … CASCADE` (`db.py:89`, controlled reset).

**Fix.** Use bound parameters: `text("INSERT INTO clinician_roster(clinician_id, patient_id) VALUES (:c, :p) ON CONFLICT DO NOTHING")` with `{"c": str(doc_id), "p": str(pid)}`, or an ORM `ClinicianRoster(...)` insert.

---

## F11 — LOW: ГОСТ process artifacts (threat model, risk register) referenced but not present

**Evidence.** `docs/02:142` (ГОСТ Р 56939) promises «threat-model перед реализацией»; `docs/02:145` (ГОСТ Р ИСО 14971) names a «файл рисков: суицидальная эскалация, ложный/пропущенный красный флаг, утечка ПДн»; `docs/02:139` references «модель угроз типов 1–3 фиксирует в файле рисков». No dedicated threat-model or risk-register artifact exists in `docs/` (no file; grep for `модель угроз|реестр рисков` matches only these forward references). `docs/05-SDD.md` covers SDD/TDD traceability (real) but contains no threat model. Notably `docs/09:61,94` self-assess «Конфиденциальность ✅» / «Защищённость ✅» — overstated given F1/F2/F3/F5/F6 above.

**Compliance.** ГОСТ Р 56939-2016 (threat modeling before implementation); ГОСТ Р ИСО 14971-2011 (risk management file for medical devices); ГОСТ Р ИСО/МЭК 12207-2010 (traceability).

**Fix.** Produce an actual threat-model document (STRIDE/data-flow for each special-category store) and a 14971 risk register with hazard→mitigation traceability, linked from `docs/05`; correct the overstated ✅ self-assessment in `docs/09`.

---

## F12 — LOW: nginx logs patient UUIDs in URIs; api `:8000` bypasses nginx TLS

**Evidence.** `ui/nginx.conf` has no `access_log` directive ⇒ default nginx combined log writes `$request` (method+URI) to stdout/log, including `/api/v1/clinician/patients/{patient_uuid}/report`. UUIDs are pseudonymous identifiers that map to subjects (still PDn under 152-ФЗ). Request/response **bodies** are not logged by nginx or the API (`main.py` has no logging middleware) — good. Separately, `infra/docker-compose.yml` maps `ports: "8000:8000"` for the `api` service, so the API is reachable directly over **plain HTTP**, bypassing nginx TLS entirely (the fxTunnel/nginx path is the documented intent, but the direct port is open).

**Compliance.** 152-ФЗ (pseudonymous identifiers still PDn); ФСТЭК №21 (TLS for transit — partially accepted as demo design).

**Fix.** Do not expose `api:8000` on the host (keep it internal to the compose network; only `ui`/nginx publishes). Configure nginx access logs to redact URI path params or restrict logging; or set `access_log off` for PDn-bearing routes in production.

---

## Prioritized Findings Table

| # | Severity | Finding | Compliance | Effort |
|---|----------|---------|------------|--------|
| F1 | **CRITICAL** | Special-category PDn plaintext at rest; AES-256/KMS claim unimplemented | 152-ФЗ art.10; ПП РФ №1119 УЗ-2; ФСТЭК №21; ГОСТ 56939 | High |
| F2 | **HIGH** | Consent revocation sets flag only; no ≤30-day data destruction | 152-ФЗ art.9 ч.2-3, art.21 | Med |
| F3 | **HIGH** | Hardcoded JWT secret/VAPID key/DB creds in compose+source → JWT forgery/auth bypass | ФСТЭК №21 ИАФ; ГОСТ 56939 | Med |
| F4 | **HIGH** | Stateless refresh JWTs: no rotation, no revocation, no logout (7-day window) | ФСТЭК №21; ГОСТ 56939 | Med |
| F5 | **MEDIUM** | 422 bodies echo `input` (passwords + diary/chat PHI) | 152-ФЗ; ФСТЭК №21 | Low |
| F6 | **MEDIUM** | Audit IP unreliable/absent; patient self-reads of PHI not audited (docs/03 claim unmet) | ФСТЭК №21; ГОСТ 56939; 152-ФЗ | Med |
| F7 | **MEDIUM** | No backup/recovery (self-acknowledged docs/09 P10) | ФСТЭК №21; ПП РФ №1119 | Med |
| F8 | **MEDIUM** | Python deps unpinned, no lockfile | ГОСТ 56939; ГОСТ 12207 | Low |
| F9 | **MEDIUM** | Access+refresh tokens in localStorage (XSS) | ФСТЭК №21; ГОСТ 56939 | Med |
| F10 | LOW | `seed.py` f-string SQL (seed-only, internal UUIDs, not API-reachable) | ГОСТ 56939 | Low |
| F11 | LOW | Threat model/risk register referenced but no artifact; overstated ✅ self-assessment | ГОСТ 56939; ГОСТ 14971; ГОСТ 12207 | Med |
| F12 | LOW | nginx logs patient UUIDs in URIs; `api:8000` direct HTTP bypasses nginx TLS | 152-ФЗ; ФСТЭК №21 | Low |

**Top remediation order:** F1 (encrypt special-category PDn at rest via real KMS) → F3 (remove committed secrets, fail-fast) → F2 (implement consent-revocation destruction) → F4 (server-side rotating refresh tokens + logout) → F5/F6/F9 (response/audit/session hardening) → F7/F8 (backup + pinned deps) → F10-F12 (process & config hygiene).

*Inference notes:* all findings are grounded in cited file:line evidence. The only inferred elements are pydantic v2's default `input` inclusion in `errors()` (well-documented library behavior confirmed by the handler code) and the nginx default-access-log behavior (standard).


---

## Remediation Status (commit 73af354 + uv F8)

| Finding | Status | Evidence (file:line) | Residual risk |
|---------|--------|----------------------|---------------|
| F1 free-text encryption | ✅ FIXED | `crypto.py:31`, `models.py:124,132,151,242,272`, mig `e6f7a8b9c0d1`+`e7a8b9c0d1e2` | Numeric scores/flags plaintext (structured, not free-text) |
| F1 Escalation.resolution | ✅ FIXED | `models.py:124` EncryptedText, mig `e7a8b9c0d1e2` | — |
| F2 consent destroy | ✅ FIXED | `consent.py:60-107` (FK-safe DELETE + push tokens) | — |
| F2 push tokens | ✅ FIXED | `consent.py:103-106` deletes PushSubscription+DeviceToken | — |
| F3 fail-fast secrets | ✅ FIXED | `config.py:31-53` inverted: always reject demo secrets unless `CC_ALLOW_DEMO_SECRETS=true` | prod deploy without flag → fail-fast at import |
| F4 refresh rotation | ✅ FIXED | `auth.py:110-114` `with_for_update()` serializes concurrent refresh | family/chain revocation not implemented (accepted MVP) |
| F5 422 strip | ✅ FIXED | `errors.py:36-40` | — |
| F6 audit IP | ✅ FIXED | `Dockerfile --proxy-headers`, nginx X-Real-IP/X-Forwarded-For, diary/chat self-read audited | — |
| F7 backup | ✅ FIXED | `tools/backup.sh` pg_dump gz + `tools/restore.sh` + 30d retention | — |
| F9 localStorage | ✅ FIXED | httpOnly cookies `cc_access`/`cc_refresh` SameSite=Lax, api.ts credentials:include | Native APK keeps localStorage (cross-origin) |
| F10 seed SQL | ✅ FIXED | `seed.py:136` parameterized text() bind params | — |
| F11 threat model | ✅ FIXED | `docs/10-модель-угроз.md` STRIDE + ГОСТ 14971 risk register R1-R12 | — |
| F12 nginx/api port | ✅ FIXED | compose `expose` (no host port), nginx `access_log safe` (no URI) | — |
| NEW-3 migration key | ⏳ ACCEPTED (LOW) | one-time migration, maintenance window, runtime uses bind param | — |

**Closed:** F1 (incl. Escalation.resolution), F2 (incl. push tokens), F3 (inverted fail-fast), F4 (with_for_update), F5, F6 (proxy-headers + audit), F7 (backup), F8, F9 (httpOnly cookies), F10 (parameterized SQL), F11 (threat model), F12 (no host port + safe logs) — **12/12 + NEW-3 accepted**. Demo-accepted residual: NEW-3 (one-time migration key), native APK localStorage (cross-origin).

## Round-3 re-audit closure (commit after d3af600)

The round-3 audit found residual gaps inside "FIXED" findings. All closed:

| Residual gap | Fix |
|--------------|-----|
| F6: clinician audit calls omitted `ip` (6 sites) | `ip` threaded from routers → `services/clinician.py` audit_record calls |
| F6: `GET /me/prescribed-scales` unaudited | READ_PRESCRIBED_SCALES audit |
| F6: `GET /admin/audit` unaudited | READ_AUDIT audit |
| F2: push/notification endpoints not consent-gated | new `require_consent_if_patient` dependency on all push/notification routes |
| F1: `AdherenceRecord.medication` plaintext (treatment context) | `EncryptedText` + migration `e8b9c0d1e2f3` |
| F7: backups were plaintext gzip | AES-256 (`openssl enc -pbkdf2`), key `CC_BACKUP_KEY`, refuses without key |
| F10: seed backdate UPDATE used f-string | parameterized `:delta`/`:rid` binds |
| F8: CI used `pip install -e`, no CVE audit | `uv sync --frozen` + `pip-audit`; workspace dev-group in `pyproject.toml` |
| F3: compose had committed secrets as the only path | `${VAR:-demo}` parameterization + `tools/gen-secrets.sh` |
| docs/02 falsely claimed AES-256 on all columns | corrected to actual pgcrypto scope + encrypted backups |
| docs/09 P10 claimed backup unimplemented | updated to implemented |

**Regression after round-3:** 62 core + 101 API + 14 e2e = **177 green**. Live-verified: migration `e8b9c0d1e2f3` applied to demo DB, e2e 14/14 through the stack.

**IDOR/cross-clinician isolation:** confirmed clean — every patient-scoped clinician function calls `_require_roster` (`clinician.py:22`); patient endpoints derive `patient_id` from JWT not path param; test `test_clinician_cannot_see_unrostered` proves 403 NO_ROSTER.

---

## Round-4 audit (post 4191df7) — NEW-1..NEW-5

Round 4 verified 10/10 prior claims PASS, and found 5 new issues. All fixed:

| ID | Severity | Issue | Fix |
|----|----------|-------|-----|
| NEW-1 | **HIGH** | Public `POST /auth/register` accepted `role=ADMIN\|CLINICIAN` with no gate → unauthenticated attacker creates ADMIN, grants roster, reads decrypted patient cards | `auth.py:_privileged_registration_allowed` — privileged roles require demo mode or an authenticated ADMIN; new operator CLI `python -m app.bootstrap_user` for first-admin bootstrap |
| NEW-2 | MEDIUM | `DiaryEntry.tags` (JSONB) plaintext — patient free text | `tags_json` maps to `EncryptedText`; `tags` property does JSON in/out; migration `e9c0d1e2f3a4` |
| NEW-3 | MEDIUM | `AdverseEvent.suspected_drug` plaintext — treatment context | `EncryptedText`, same migration |
| NEW-4 | MEDIUM | Postgres/Qdrant published on `0.0.0.0` with default creds | base compose uses `expose` only; `infra/docker-compose.dev.yml` publishes to `127.0.0.1` for dev/CI |
| NEW-5 | MEDIUM | compose defaulted `CC_ALLOW_DEMO_SECRETS=true`, defeating F3 | default is now `false` (fail-fast); demo needs explicit `.env` (`infra/.env.example`) |

**Regression after round 4:** 67 core + 104 API + 14 e2e = **185 green**. Live: migrations `e9c0d1e2f3a4` applied, demo reseeded, e2e 14/14.

### Round-5 verification — CLEAN

Round 5 (8m30s, 10 files) verified all 5 round-4 fixes: **5/5 PASS, no new CRITICAL/HIGH/MEDIUM**.

Two LOW observations were actionable and are now fixed:

| LOW | Issue | Fix |
|-----|-------|-----|
| tags setter | `entry.tags = []` wrote `None` into a NOT NULL column → IntegrityError | setter always serialises `"[]"` |
| data_key gating | demo `CC_DATA_KEY` was only rejected under `prod_mode`, so a strong JWT secret could mask a known encryption key | demo data key now rejected whenever `allow_demo_secrets` is false |

Remaining LOW (accepted): migration key literal in `ALTER ... USING` (one-off DDL, operator-controlled, documented); demo-mode registration of privileged roles (by design, requires explicit `CC_ALLOW_DEMO_SECRETS=true`).

**Final regression:** 67 core + 104 API + 14 e2e = **185 green**. Live stack: only `ui:8090` published; postgres/qdrant/api internal.
