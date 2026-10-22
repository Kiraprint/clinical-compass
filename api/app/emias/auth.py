"""Кэш OAuth-токена ЕМИАС (client_credentials).

Токен обновляется заранее — на 80% заявленного expires_in (REFRESH_RATIO),
чтобы не ловить 401 на границе жизни токена. Часы инъектируются (monotonic
по умолчанию) — политика обновления тестируется без сна.
"""

from __future__ import annotations

from dataclasses import dataclass

REFRESH_RATIO = 0.8

@dataclass(frozen=True)
class CachedToken:
    access_token: str
    expires_in: float
    obtained_at: float
    scopes: tuple[str, ...] = ()

    def refresh_at(self) -> float:
        return self.obtained_at + max(0.0, self.expires_in) * REFRESH_RATIO

    def stale(self, now: float) -> bool:
        """Пора ли запрашивать новый токен (80% TTL или истёк)."""
        return now >= self.refresh_at()

class TokenCache:
    def __init__(self, clock) -> None:
        self._clock = clock
        self._token: CachedToken | None = None

    def get(self) -> CachedToken | None:
        return self._token

    def put(self, access_token: str, expires_in: float, scopes=()) -> CachedToken:
        self._token = CachedToken(
            access_token=access_token,
            expires_in=float(expires_in),
            obtained_at=self._clock(),
            scopes=tuple(scopes),
        )
        return self._token

    def clear(self) -> None:
        self._token = None
