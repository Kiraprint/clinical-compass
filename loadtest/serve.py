"""Запуск изолированного API-процесса под нагрузочные замеры."""

from __future__ import annotations

import subprocess
import sys
import time

from . import common


class ApiServer:
    def __init__(
        self,
        *,
        embedder: str = "hash",
        collection: str | None = None,
        db: str | None = None,
        port: int = common.API_PORT,
        pool_size: int = 0,
    ):
        self.embedder = embedder
        self.collection = collection
        self.db = db
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.log_path = common.RESULTS_DIR / "api.log"
        self.collection = common.resolve_collection(collection)
        self.db = common.resolve_db(db)
        self.pool_size = pool_size

    def start(self, timeout: float = 120.0) -> "ApiServer":
        env = common.setup_env(
            embedder=self.embedder, collection=self.collection, db=self.db
        )
        env["CC_LOADTEST_POOL_SIZE"] = str(self.pool_size)
        env["PYTHONPATH"] = f"{common.ROOT}:{env['PYTHONPATH']}"
        log = self.log_path.open("w", encoding="utf-8")
        self._log = log
        cmd = [sys.executable, "-m", "loadtest.server_app"]
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(common.API_DIR),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self._wait_health(timeout)
        self.startup_s = time.perf_counter() - t0
        return self

    def _wait_health(self, timeout: float) -> None:
        import httpx

        deadline = time.perf_counter() + timeout
        last_error: Exception | None = None
        while time.perf_counter() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"API завершился при старте (code={self.proc.returncode}):\n{self.log_tail()}"
                )
            try:
                response = httpx.get(f"{self.base_url}/health", timeout=1.0)
                if response.status_code == 200:
                    return
            except Exception as exc:  # noqa: BLE001 — сервер ещё поднимается
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(f"API не поднялся за {timeout}s: {last_error}\n{self.log_tail()}")

    def log_tail(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8").splitlines()[-lines:])

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if getattr(self, "_log", None) is not None:
            self._log.close()

    def __enter__(self) -> "ApiServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
