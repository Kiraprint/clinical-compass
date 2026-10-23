"""Isolated provisioning for load tests.

Creates database `compass_loadtest`, migrates schema, seeds users/consents/diary,
and indexes both the app KB corpus and the bench golden corpus into Qdrant
collection `kb_loadtest`. Nothing here touches the demo/prod databases.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from pathlib import Path

from . import common


def _psycopg():
    import psycopg

    return psycopg


def reset_database() -> None:
    psycopg = _psycopg()
    with psycopg.connect(f"{common.PG_DSN} dbname=postgres", autocommit=True) as conn:
        conn.execute(
            f'DROP DATABASE IF EXISTS "{common.DB_NAME}" WITH (FORCE)'
        )
        conn.execute(f'CREATE DATABASE "{common.DB_NAME}"')


def init_schema() -> None:
    os.environ.update(common.setup_env())
    from app import db

    db.setup(common.DB_URL)
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


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def seed_qdrant() -> dict:
    """Index app KB + bench golden corpus into the isolated collection."""
    os.environ.update(common.setup_env())
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
    if client.collection_exists(kb.COLLECTION):
        client.delete_collection(kb.COLLECTION)
    kb.ensure_collection(client, embedder.dense_size)
    indexed = kb.upsert_documents(client, embedder, docs)
    points = kb.count_points(client)
    return {
        "collection": kb.COLLECTION,
        "embedder": type(embedder).__name__,
        "dense_size": embedder.dense_size,
        "documents": len(docs),
        "app_documents": len(app_docs),
        "bench_documents": len(bench_docs),
        "upserted": indexed,
        "points": points,
    }


def provision(embedder: str = "hash", collection: str = common.QDRANT_COLLECTION) -> dict:
    os.environ.update(common.setup_env(embedder=embedder, collection=collection))
    reset_database()
    init_schema()
    creds = seed_users_and_data()
    qdrant = seed_qdrant()
    payload = {
        "database": common.DB_NAME,
        "users": len(creds["patients"]) + len(creds["clinicians"]),
        "patients": len(creds["patients"]),
        "clinicians": len(creds["clinicians"]),
        "qdrant": qdrant,
    }
    common.write_json("provision.json", payload)
    return payload


def load_credentials() -> dict:
    path = common.RESULTS_DIR / "credentials.json"
    if not path.exists():
        raise SystemExit("Нет credentials.json — сначала выполните provision")
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    print(json.dumps(provision(), ensure_ascii=False, indent=2))
