"""Настройки исходящей передачи СЭМД в РЭМД/ЕГИСЗ (FR-41, docs/18 §8).

Как и `app.emias.config`: значения приходят из `app.config.Settings`
(`CC_REMD_*`), здесь — снимок и проверки до первого сетевого запроса:

  * схема http допустима только при `CC_REMD_ALLOW_INSECURE=true` (мок);
  * netloc без userinfo/query/fragment — основа белого списка хостов;
  * включённая интеграция требует client_id/client_secret, `CC_REMD_LPU_UID`,
    `CC_REMD_CALLBACK_URL` с разрешённой схемой и непустого callback-токена;
  * демо-секрет мока запрещён вне контура мока (иначе известный из git секрет
    оказался бы в живом контуре).

Egress по умолчанию выключен (`CC_REMD_ENABLED=false`, docs/10 R17).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config import settings

DEMO_SECRETS = frozenset({"remd-secret"})  # креды remd-mock из compose
DEMO_CALLBACK_TOKENS = frozenset({"remd-callback-token"})  # токен callback из compose
CALLBACK_HEADER = "X-Remd-Token"


class RemdConfigError(ValueError):
    """Некорректная конфигурация: интеграция не должна стартовать."""


@dataclass(frozen=True)
class RemdConfig:
    enabled: bool
    base_url: str
    client_id: str
    # repr=False: секреты не должны попадать в трейсбеки/data-class repr.
    client_secret: str = field(repr=False)
    lpu_uid: str = ""
    callback_url: str = ""
    callback_token: str = field(default="", repr=False)
    callback_max_kb: int = 256
    allow_insecure: bool = False
    timeout: float = 10.0
    max_attempts: int = 3
    max_response_bytes: int = 4 * 1024 * 1024
    retry_after_cap: float = 60.0
    sending_stale_sec: int = 900
    patient_snils: str = field(default="", repr=False)
    document_kind: int = 34
    org_name: str = ""
    department_oid: str = ""
    department_name: str = ""

    @classmethod
    def from_settings(cls, source=settings) -> "RemdConfig":
        return cls(
            enabled=source.remd_enabled,
            base_url=source.remd_base_url,
            client_id=source.remd_client_id,
            client_secret=source.remd_client_secret,
            lpu_uid=source.remd_lpu_uid,
            callback_url=source.remd_callback_url,
            callback_token=source.remd_callback_token,
            callback_max_kb=source.remd_callback_max_kb,
            allow_insecure=source.remd_allow_insecure,
            timeout=source.remd_timeout,
            max_attempts=source.remd_max_attempts,
            max_response_bytes=source.remd_max_response_bytes,
            retry_after_cap=source.remd_retry_after_cap,
            sending_stale_sec=source.remd_sending_stale_sec,
            patient_snils=source.remd_patient_snils,
            document_kind=source.remd_document_kind,
            org_name=source.remd_org_name,
            department_oid=source.remd_department_oid,
            department_name=source.remd_department_name,
        )

    # --- проверки -----------------------------------------------------------

    def parsed(self):
        return urlparse(self.base_url.strip())

    def validate(self) -> None:
        """Fail-fast на старте клиента/роутера (не на импорте модуля)."""
        parts = self.parsed()
        if parts.scheme not in ("http", "https"):
            raise RemdConfigError("CC_REMD_BASE_URL: схема должна быть http/https")
        if not parts.netloc:
            raise RemdConfigError("CC_REMD_BASE_URL: пустой хост")
        if "@" in parts.netloc:
            raise RemdConfigError("CC_REMD_BASE_URL: userinfo в URL запрещён")
        if parts.query or parts.fragment:
            raise RemdConfigError("CC_REMD_BASE_URL: query/fragment в базовом URL запрещены")
        if parts.scheme == "http" and not self.allow_insecure:
            raise RemdConfigError(
                "CC_REMD_BASE_URL: http допустим только при CC_REMD_ALLOW_INSECURE=true (мок)"
            )
        if self.timeout <= 0:
            raise RemdConfigError("CC_REMD_TIMEOUT должен быть > 0")
        if self.max_attempts < 1:
            raise RemdConfigError("CC_REMD_MAX_ATTEMPTS должен быть >= 1")
        if self.max_response_bytes <= 0:
            raise RemdConfigError("CC_REMD_MAX_RESPONSE_BYTES должен быть > 0")
        if self.retry_after_cap <= 0:
            raise RemdConfigError("CC_REMD_RETRY_AFTER_CAP должен быть > 0")
        if self.sending_stale_sec < 0:
            raise RemdConfigError("CC_REMD_SENDING_STALE_SEC должен быть >= 0")
        if self.callback_max_kb <= 0:
            raise RemdConfigError("CC_REMD_CALLBACK_MAX_KB должен быть > 0")
        if not self.enabled:
            return
        if not self.client_id or not self.client_secret:
            raise RemdConfigError("CC_REMD_CLIENT_ID/CC_REMD_CLIENT_SECRET не заданы")
        if not self.lpu_uid:
            raise RemdConfigError("CC_REMD_LPU_UID не задан")
        if not self.callback_token:
            raise RemdConfigError("CC_REMD_CALLBACK_TOKEN не задан (callback без аутентификации)")
        callback = urlparse(self.callback_url.strip())
        if callback.scheme not in ("http", "https") or not callback.netloc:
            raise RemdConfigError("CC_REMD_CALLBACK_URL: нужен абсолютный http(s) URL")
        if callback.scheme == "http" and not self.allow_insecure:
            raise RemdConfigError(
                "CC_REMD_CALLBACK_URL: http допустим только при CC_REMD_ALLOW_INSECURE=true"
            )
        if not self.allow_insecure and (
            self.client_secret in DEMO_SECRETS
            or self.callback_token in DEMO_CALLBACK_TOKENS
        ):
            # Оба демо-значения есть в git (compose): вне контура мока это
            # известный секрет, а не секрет.
            raise RemdConfigError(
                "CC_REMD_CLIENT_SECRET/CC_REMD_CALLBACK_TOKEN: демо-значения из git "
                "допустимы только с CC_REMD_ALLOW_INSECURE=true"
            )
        if self.document_kind <= 0:
            raise RemdConfigError("CC_REMD_DOCUMENT_KIND должен быть положительным кодом НСИ 1520")

    def host_allowed(self, url: str) -> bool:
        """Белый список: только схема/netloc из CC_REMD_BASE_URL (docs/10 R17)."""
        parts = urlparse(url)
        base = self.parsed()
        if parts.scheme != base.scheme:
            return False
        if parts.netloc.lower() != base.netloc.lower():
            return False
        if parts.scheme == "http" and not self.allow_insecure:
            return False
        return True


def current_config() -> RemdConfig:
    """Снимок текущих настроек (читаются на каждый вызов — тесты/CLI)."""
    return RemdConfig.from_settings()
