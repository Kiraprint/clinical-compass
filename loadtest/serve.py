"""Запуск изолированного API-процесса под нагрузочные замеры.

`workers>1` поднимает многопроцессный uvicorn (`--workers N` + WEB_CONCURRENCY=N);
приложение стартует из строки импорта `app.main:app`, иначе uvicorn отказывается
работать в многопроцессном режиме. Пул БД задаётся на процесс, а бюджет
соединений считается как N × (pool + overflow + 2) ≤ CC_DB_MAX_CONNECTIONS
(`common.pool_budget`); каждый воркер проверяет его сам в lifespan и падает на
старте с точной конфигурацией, а не деградирует молча.
"""

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
        max_overflow: int | None = None,
        workers: int = 1,
        log_name: str | None = None,
    ):
        self.embedder = embedder
        self.collection = common.resolve_collection(collection)
        # Имя БД резолвится здесь же: снимки pg_stat_activity в run_workers
        # фильтруют по self.db, и «сырое» None молча давало нули в отчёте.
        self.db = common.resolve_db(db)
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.log_path = common.RESULTS_DIR / (log_name or "api.log")
        self.pool_size = pool_size
        # Явный overflow важнее производной pool//4 (так же считает server_app):
        # pool-профиль run_cliff полагается на исторические 60+15.
        if max_overflow is not None:
            self.max_overflow = max_overflow
        elif pool_size:
            self.max_overflow = max(1, pool_size // 4)
        else:
            self.max_overflow = common.DEFAULT_MAX_OVERFLOW
        self.workers = max(1, workers)
        self.budget = common.pool_budget(
            self.workers,
            self.pool_size or common.DEFAULT_POOL_SIZE,
            self.max_overflow,
        )
        self.startup_s = 0.0

    def start(self, timeout: float = 120.0) -> "ApiServer":
        # Конфигурация проверяется до спавна: отказ бюджета должен называть
        # цифры, а не «API завершился при старте».
        if not self.budget["fits"]:
            raise RuntimeError(f"Пул БД не влезает в Postgres: {self.budget['formula']}")

        env = common.setup_env(
            embedder=self.embedder, collection=self.collection, db=self.db
        )
        env["CC_LOADTEST_POOL_SIZE"] = str(self.pool_size)
        if self.pool_size:
            env["CC_LOADTEST_MAX_OVERFLOW"] = str(self.max_overflow)
        env["WEB_CONCURRENCY"] = str(self.workers)
        # Порт экземпляра, а не импортный: несколько стендов на одной машине.
        env["CC_LOADTEST_PORT"] = str(self.port)
        # В изолированном контуре не запущены reminders-worker/kb-enrichment,
        # поэтому их пулы не резервируем: иначе validate_pool_budget откажет
        # старту при увеличенном пуле API.
        env["CC_DB_BACKGROUND_PROCESSES"] = "0"
        env["PYTHONPATH"] = f"{common.ROOT}:{env['PYTHONPATH']}"
        log = self.log_path.open("w", encoding="utf-8")
        self._log = log
        cmd = [
            sys.executable, "-m", "loadtest.server_app",
            "--workers", str(self.workers),
            "--port", str(self.port),
        ]
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

    def worker_pids(self) -> list[int]:
        """PID дерева сервера (supervisor + воркеры, без resource_tracker)."""
        return common.uvicorn_pids(self.proc.pid)

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
