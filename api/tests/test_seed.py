"""Сид демо-данных не разрушает прод-состояние (docs/03 §13).

Регрессия на реальный инцидент: `docker compose up`, пересоздавший one-shot
сервис `seed`, запускал `app.seed --reset` и обнулял демо-данные вместе с
реестром КБ (`kb_works`, `kb_work_approvals`). Теперь сид идемпотентен:
учётки ищутся по email и не перезаписываются, пациенты не дублируются,
`--reset` остаётся ручным.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import func, select

from app import db
from app.models import KBWork, Run, User

API_DIR = Path(__file__).resolve().parents[1]


def _run_seed(url: str, patients: int = 3) -> str:
    env = {
        **os.environ,
        "CC_ALLOW_DEMO_SECRETS": "true",
        "PYTHONPATH": f"{API_DIR}:{API_DIR.parent / 'core'}",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "app.seed", "--url", url, "--patients", str(patients)],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def test_seed_twice_preserves_registry_and_users(db_session):
    """Повторный сид: пользователи/реестр/история на месте, TOTP не меняется."""
    url = os.environ.get(
        "CC_TEST_DATABASE_URL",
        "postgresql+psycopg://compass:compass@127.0.0.1:5432/compass_test",
    )

    # «Прод-состояние», которое сид не имеет права стереть.
    db_session.add(
        KBWork(
            kind="article",
            source="pubmed",
            external_id="x-sentinel",
            guideline_id="gid-sentinel",
            revision="r1",
            content_hash="h1",
            status="candidate",
            title="SENTINEL",
            full_text="text",
        )
    )
    db_session.commit()

    _run_seed(url)
    db_session.expire_all()
    totp_first = db_session.scalar(select(User.totp_secret).where(User.email == "doc@compass.demo"))
    users_first = db_session.scalar(select(func.count()).select_from(User))
    # Первый прогон на пустой БД создаёт демо-историю — это нормально.
    runs_first = db_session.scalar(select(func.count()).select_from(Run))
    assert runs_first > 0

    # Второй прогон: тот же --patients, ничего не пересоздаётся.
    _run_seed(url)
    db_session.expire_all()

    assert db_session.scalar(select(func.count()).select_from(User)) == users_first
    assert db_session.scalar(select(User.totp_secret).where(User.email == "doc@compass.demo")) == totp_first
    assert db_session.scalar(select(func.count()).select_from(KBWork).where(KBWork.title == "SENTINEL")) == 1
    assert db_session.scalar(select(func.count()).select_from(Run)) == runs_first, "история не перегенерирована"
