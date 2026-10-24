"""Isolated provisioning for load tests.

Creates database `compass_loadtest` (allowlist: `compass_load*`), migrates schema,
seeds users/consents/diary + the drugs reference table, and indexes both the app
KB corpus (including drug chunks) and the bench golden corpus into Qdrant
collection `kb_loadtest` (allowlist: `kb_load*`).

Destructive by design (`DROP DATABASE ... WITH FORCE`, collection re-create), so
it refuses to run unless CC_LOADTEST_ALLOW=1 and both target names match the
allowlist — checked before any connection is opened.

`--dry-run` prints the resolved targets without connecting.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from pathlib import Path

if __package__ in (None, ""):  # python loadtest/provision.py --help / --dry-run
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from loadtest import common
else:
    from . import common


def _psycopg():
    import psycopg

    return psycopg


def reset_database(db_name: str | None = None) -> None:
    db_name, _ = common.assert_destructive(db_name, action="reset_database")
    from psycopg import sql

    psycopg = _psycopg()
    dsn = (
        f"host={common.PG_HOST} port={common.PG_PORT} user={common.PG_USER} "
        f"password={common.PG_PASSWORD} dbname=postgres"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Identifier, а не f-строка: имя приходит из CLI/env, allowlist проверяет
        # только префикс, а simple query protocol выполнит и второй оператор.
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(db_name)
        ))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))


def init_schema() -> None:
    from app import db

    db.setup(common.db_url())
    db.init_db()


def seed_users_and_data() -> dict:
    """Users, consents, diary history; returns access tokens for the API process."""
    from app import db, security
    from app.models import ClinicianRoster, Consent, DiaryEntry, User

    assert db.SyncSessionLocal is not None
    password_hash = security.hash_password(common.PASSWORD)

    patients: list[User] = []
    clinicians: list[User] = []

    with db.SyncSessionLocal() as session:
        for i in range(common.PATIENTS):
            patient = User(
                id=uuid.uuid4(),
                email=f"load-patient-{i:04d}@example.test",
                password_hash=password_hash,
                role="PATIENT",
                fio=f"Нагрузочный Пациент {i:04d}",
            )
            session.add(patient)
            patients.append(patient)

        for i in range(common.CLINICIANS):
            clinician = User(
                id=uuid.uuid4(),
                email=f"load-doctor-{i:04d}@example.test",
                password_hash=password_hash,
                role="CLINICIAN",
                fio=f"Нагрузочный Врач {i:04d}",
                totp_secret=security.generate_totp_secret(),
            )
            session.add(clinician)
            clinicians.append(clinician)

        session.flush()
        for patient in patients:
            session.add(Consent(user_id=patient.id, version=1, text_hash="consent-v1"))

        session.flush()

        # Каждому врачу — 8 пациентов в ростере (для dashboard/roster-запросов).
        for ci, clinician in enumerate(clinicians):
            for patient in patients[ci * 8 : ci * 8 + 8]:
                session.add(
                    ClinicianRoster(clinician_id=clinician.id, patient_id=patient.id)
                )

        # История дневника: 5 записей на пациента → непустые выборки под чтением.
        rng = random.Random(42)
        for patient in patients:
            for day in range(5):
                session.add(
                    DiaryEntry(
                        patient_id=patient.id,
                        content=f"Нагрузочная запись {day}: самочувствие стабильное, сон 7 часов.",
                        mood=rng.randint(0, 4),
                        mood_scale="0-4",
                        sleep_h=round(rng.uniform(4.5, 9.0), 1),
                        tags_json=json.dumps(["нагрузка", f"день-{day}"], ensure_ascii=False),
                    )
                )

        session.commit()

        credentials = {
            "patients": [
                {
                    "id": str(u.id),
                    "email": u.email,
                    "token": security.create_access_token(u.id, u.role),
                }
                for u in patients
            ],
            "clinicians": [
                {
                    "id": str(u.id),
                    "email": u.email,
                    "token": security.create_access_token(u.id, u.role),
                }
                for u in clinicians
            ],
            "password": common.PASSWORD,
        }

    common.write_json("credentials.json", credentials)
    return credentials


def seed_drugs() -> int:
    """Load api/app/data/drugs.json into `drugs` (same path as app.seed._seed_drugs).

    Needs to happen BEFORE seed_qdrant(): kb_seed._drug_docs builds drug chunks
    from this table, so without rows the agent KB silently lacks drugs.
    """
    from sqlalchemy import func, select

    from app import db
    from app.models import Drug
    from app.seed import _seed_drugs

    assert db.SyncSessionLocal is not None
    with db.SyncSessionLocal() as session:
        _seed_drugs(session)
        session.commit()
        return int(session.scalar(select(func.count()).select_from(Drug)) or 0)


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def seed_qdrant(collection: str | None = None) -> dict:
    """Index app KB + bench golden corpus into the isolated collection."""
    # Проверяем ровно ту коллекцию, которую получили, а не резолвим заново из env.
    _, collection = common.assert_destructive(collection, action="seed_qdrant")
    from qdrant_client import QdrantClient

    from app.kb_seed import build_corpus
    from app.services import kb
    from app.services.embeddings import build_embedder

    app_docs = build_corpus()
    bench_path = common.bench_data_dir() / "corpus.jsonl"
    bench_docs = [
        {
            "guideline_id": row["id"],
            "source_title": row.get("source", row["id"]),
            "topic": row.get("topic", "metaanalysis"),
            "text": row["text"],
        }
        for row in load_jsonl(bench_path)
    ]
    docs = list(app_docs) + bench_docs

    embedder = build_embedder()
    client = QdrantClient(url=common.QDRANT_URL)
    if client.collection_exists(collection):
        client.delete_collection(collection)
    kb.ensure_collection(client, embedder.dense_size)
    indexed = kb.upsert_documents(client, embedder, docs)
    points = kb.count_points(client)
    return {
        "collection": collection,
        "embedder": type(embedder).__name__,
        "device": str(getattr(embedder, "device", "cpu")),
        "dense_size": embedder.dense_size,
        "documents": len({str(d["guideline_id"]) for d in docs}),
        "chunks": len(docs),
        "app_chunks": len(app_docs),
        "bench_chunks": len(bench_docs),
        "drug_chunks": sum(
            1 for d in app_docs if str(d["guideline_id"]).startswith("drug-")
        ),
        "upserted": indexed,
        "points": points,
    }


def provision(
    embedder: str = "hash",
    collection: str | None = None,
    db: str | None = None,
) -> dict:
    db_name, coll = common.assert_destructive(db, collection, action="provision")
    common.apply_env(embedder=embedder, collection=coll, db=db_name)
    reset_database(db_name)
    init_schema()
    creds = seed_users_and_data()
    drugs = seed_drugs()
    qdrant = seed_qdrant(coll)
    payload = {
        "database": db_name,
        "collection": coll,
        "embedder": embedder,
        "users": len(creds["patients"]) + len(creds["clinicians"]),
        "patients": len(creds["patients"]),
        "clinicians": len(creds["clinicians"]),
        "drugs": drugs,
        "qdrant": qdrant,
    }
    common.write_json("provision.json", payload)
    return payload


def load_credentials() -> dict:
    path = common.RESULTS_DIR / "credentials.json"
    if not path.exists():
        raise SystemExit("Нет credentials.json — сначала выполните provision")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Изолированный provision нагрузочного стенда Clinical Compass"
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"имя БД (allowlist: {common.DB_PREFIX}*); по умолчанию "
        f"CC_LOADTEST_DB или {common.DEFAULT_DB}",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help=f"коллекция Qdrant (allowlist: {common.COLLECTION_PREFIX}*); по умолчанию "
        f"CC_LOADTEST_COLLECTION или {common.DEFAULT_COLLECTION}",
    )
    parser.add_argument("--embedder", default="hash", choices=["hash", "bge-m3"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать разрешённые БД/коллекцию и выйти, не подключаясь",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        print(common.describe_target(args.db, args.collection))
        if args.dry_run:
            return 0
        payload = provision(
            embedder=args.embedder, collection=args.collection, db=args.db
        )
    except common.SafetyRefusal as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return common.SafetyRefusal.exit_code
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
