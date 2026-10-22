"""Настройки интеграции с ЕМИАС (FR-25).

Все значения приходят из app.config.Settings (env CC_EMIAS_*), здесь — только
неизменяемый снимок и проверки, которые нужны до первого сетевого запроса:

  * схема: https обязателен, кроме явного CC_EMIAS_ALLOW_INSECURE=true (локальный мок);
  * netloc без userinfo/query/fragment — база для белого списка хостов;
  * включённая интеграция требует непустых client_id/client_secret.

Egress по умолчанию выключен (CC_EMIAS_ENABLED=false, docs/10 R17).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import settings

SCOPES = ("patient.read", "encounter.read", "medication.read")

class EmiasConfigError(ValueError):
    """Некорректная конфигурация: интеграция не должна стартовать."""

@dataclass(frozen=True)
class EmiasConfig:
    enabled: bool
    base_url: str
    client_id: str
    client_secret: str
    allow_insecure: bool = False
    timeout: float = 10.0
    max_attempts: int = 3
    max_response_bytes: int = 4 * 1024 * 1024
    snils_pepper: str = ""

    @classmethod
    def from_settings(cls, source=settings) -> "EmiasConfig":
        return cls(
            enabled=source.emias_enabled,
            base_url=source.emias_base_url,
            client_id=source.emias_client_id,
            client_secret=source.emias_client_secret,
            allow_insecure=source.emias_allow_insecure,
            timeout=source.emias_timeout,
            max_attempts=source.emias_max_attempts,
            max_response_bytes=source.emias_max_response_bytes,
            # Перец: отдельный секрет, иначе ключ шифрования ПДн (он и так вне git).
            snils_pepper=source.emias_snils_pepper or source.data_key,
        )

    # --- проверки -----------------------------------------------------------

    def parsed(self):
        return urlparse(self.base_url.strip())

    def validate(self) -> None:
        """Fail-fast на старте клиента/роутера (не на импорте модуля)."""
        parts = self.parsed()
        if parts.scheme not in ("http", "https"):
            raise EmiasConfigError("CC_EMIAS_BASE_URL: схема должна быть http/https")
        if not parts.netloc:
            raise EmiasConfigError("CC_EMIAS_BASE_URL: пустой хост")
        if "@" in parts.netloc:
            raise EmiasConfigError("CC_EMIAS_BASE_URL: userinfo в URL запрещён")
        if parts.query or parts.fragment:
            raise EmiasConfigError("CC_EMIAS_BASE_URL: query/fragment в базовом URL запрещены")
        if parts.scheme == "http" and not self.allow_insecure:
            raise EmiasConfigError(
                "CC_EMIAS_BASE_URL: http допустим только при CC_EMIAS_ALLOW_INSECURE=true (мок)"
            )
        if self.enabled and (not self.client_id or not self.client_secret):
            raise EmiasConfigError("CC_EMIAS_CLIENT_ID/CC_EMIAS_CLIENT_SECRET не заданы")
        if self.max_attempts < 1:
            raise EmiasConfigError("CC_EMIAS_MAX_ATTEMPTS должен быть >= 1")
        if self.max_response_bytes <= 0:
            raise EmiasConfigError("CC_EMIAS_MAX_RESPONSE_BYTES должен быть > 0")

    def host_allowed(self, url: str) -> bool:
        """Белый список: только netloc/схема из CC_EMIAS_BASE_URL (docs/10 R17)."""
        parts = urlparse(url)
        base = self.parsed()
        if parts.scheme != base.scheme:
            return False
        if parts.netloc.lower() != base.netloc.lower():
            return False
        if parts.scheme == "http" and not self.allow_insecure:
            return False
        return True

def current_config() -> EmiasConfig:
    """Снимок текущих настроек (значения читаются на каждый вызов — тесты/CLI)."""
    return EmiasConfig.from_settings()
