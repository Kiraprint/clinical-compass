"""Симулятор демо-данных (FR-14): детерминированный генератор пациентов.

Реалистичная динамика: базовый уровень + тренд + шум; 1–2 пациента с RED-траекторией
(суицидальный пункт / высокие баллы) для демонстрации эскалаций.
Запуск: python -m app.seed --reset --patients 12 --days 14
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text

from app import db, security
from app.models import Answer, Assessment, Consent, DiaryEntry, Drug, PatientAnamnesis, Prescription, Run, User
from app.services import screening as screening_service

MOSCOW_TZ = timezone(timedelta(hours=3))

DEMO_TOTP = os.environ.get("CC_DEMO_TOTP", "")  # фиксированный 2FA для демо/e2e (иначе случайный)

# Контуры пациентов: (имя, шкала, базовый балл, тренд в день, шум, суицидальный день)
PATIENT_PROFILES = [
    ("Анна С.", "PHQ-9", 6, -0.3, 1.2, None),      # улучшение
    ("Игорь К.", "GAD-7", 12, 0.4, 1.0, None),     # ухудшение
    ("Мария Л.", "PHQ-9", 16, -0.2, 0.8, 11),      # RED: суицидальный пункт на 11-й день
    ("Павел М.", "GAD-7", 4, 0.1, 0.9, None),      # стабильно зелёный
    ("Ольга Н.", "PHQ-9", 10, -0.1, 1.1, None),
    ("Дмитрий Р.", "GAD-7", 8, 0.15, 1.0, None),
    ("Елена В.", "PHQ-9", 13, 0.25, 1.0, None),    # к RED по баллу
    ("Сергей Т.", "GAD-7", 5, -0.05, 0.9, None),
    ("Наталья П.", "PHQ-9", 9, 0.05, 1.2, None),
    ("Виктор Ж.", "GAD-7", 7, -0.2, 1.0, None),
    ("Ксения А.", "PHQ-9", 3, 0.0, 0.7, None),
    ("Роман Д.", "GAD-7", 11, -0.35, 1.0, 8),      # RED на 8-й день
]


def _q9_value(profile, day: int) -> int:
    if profile[4] is None or day != profile[4]:
        return 0
    return 1 if profile[1] == "PHQ-9" else 0


def _answers_for(profile, rng: random.Random, total: int, day: int, max_value: int = 3) -> dict[str, int]:
    code = profile[1]
    keys = [f"q{i}" for i in range(1, 10)] if code == "PHQ-9" else [f"q{i}" for i in range(1, 8)]
    answers: dict[str, int] = {}
    rem = total
    for key in keys:
        take = min(max_value, rem)
        answers[key] = take
        rem -= take
    if code == "PHQ-9":
        answers["q9"] = _q9_value(profile, day)
        # перераспределим, чтобы total был близок к целевому
    return answers


def _drug_payload() -> list[dict] | None:
    p = Path(__file__).parent / "data" / "drugs.json"
    if not p.exists():
        print("[seed] drugs.json не найден, пропуск справочника препаратов")
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("drugs", [])


def _drug_rows(payload: list[dict]) -> list[Drug]:
    return [
        Drug(
            drug_id=d["drug_id"], trade_name=d["trade_name"], inn=d["inn"],
            inn_en=d.get("inn_en"), form=d["form"], reg_certificate=d["reg_certificate"],
            reg_date=datetime.strptime(d["reg_date"], "%Y-%m-%d").date(),
            revision_date=datetime.strptime(d["revision_date"], "%Y-%m-%d").date() if d.get("revision_date") else None,
            pharmacology=d["pharmacology"], indications=d["indications"], dosage=d["dosage"],
            contraindications=d["contraindications"], adverse_events=d["adverse_events"],
            special_notes=d["special_notes"], source_urls=d.get("source_urls") or [],
        )
        for d in payload
    ]

# Поля справочника, переносимые из drugs.json в строку drugs (кроме id,
# drug_id и created_at).
_DRUG_FIELDS = (
    "trade_name", "inn", "inn_en", "form", "reg_certificate", "reg_date",
    "revision_date", "pharmacology", "indications", "dosage",
    "contraindications", "adverse_events", "special_notes", "source_urls",
)


def _upsert_drugs(s, existing: dict[str, Drug], rows: list[Drug]) -> None:
    """Upsert по drug_id: обновляет совпавшие строки, добавляет новые, удаляет выбывшие.

    Обновление вместо DELETE+INSERT: на drugs.drug_id ссылается
    adverse_events.drug_id, поэтому повторный сид не должен ни ломать FK, ни
    менять id строк (id стабилен между прогонами).
    """
    for row in rows:
        current = existing.pop(row.drug_id, None)
        if current is None:
            s.add(row)
            continue
        for field in _DRUG_FIELDS:
            setattr(current, field, getattr(row, field))
    # Препарат убрали из drugs.json — из справочника он тоже уходит.
    for stale in existing.values():
        s.delete(stale)


def _seed_drugs(s) -> None:
    """Загружает api/app/data/drugs.json в drugs (sync: тесты/схема, не request-path).

    Idempotent: upsert по drug_id, повторный запуск не создаёт дубликатов. Таблица —
    источник правды для чанков ИИ-агента (kb_seed._drug_docs) и для API /drugs.
    """
    payload = _drug_payload()
    if payload is None:
        return
    existing = {d.drug_id: d for d in s.execute(select(Drug)).scalars()}
    _upsert_drugs(s, existing, _drug_rows(payload))
    s.flush()
    # drugs — источник чанков корпуса (kb_seed._drug_docs): сбрасываем кеш корпуса,
    # иначе python -m app.kb_seed / тесты увидят старые чанки до истечения TTL.
    from app.kb_seed import clear_corpus_cache

    clear_corpus_cache()
    print(f"[seed] drugs: {len(payload)} в таблице (источник для KB-чанков и /drugs)")


async def _seed_drugs_async(s) -> None:
    """То же, что _seed_drugs, но через async-сессию (seed.py)."""
    payload = _drug_payload()
    if payload is None:
        return
    existing = {d.drug_id: d for d in (await s.execute(select(Drug))).scalars()}
    _upsert_drugs(s, existing, _drug_rows(payload))
    await s.flush()
    # См. _seed_drugs: срез препаратов в кеше корпуса устарел.
    from app.kb_seed import clear_corpus_cache

    clear_corpus_cache()
    print(f"[seed] drugs: {len(payload)} в таблице (источник для KB-чанков и /drugs)")


async def _ensure_user(s, email: str, *, fio: str, role: str, totp_secret: str):
    """Get-or-create демо-учётки: повторный сид не ломает вход и не крутит TOTP.

    Пароль/TOTP существующей учётки не перезаписываются: иначе деплой,
    пересоздавший one-shot сервис `seed`, менял бы демо-2FA и все проверки
    (и e2e) начинали падать с 401.
    """
    user = await s.scalar(select(User).where(User.email == email))
    if user is not None:
        return user
    user = User(
        email=email,
        fio=fio,
        password_hash=security.hash_password("Demo-Pass-1"),
        role=role,
        totp_secret=totp_secret,
    )
    s.add(user)
    await s.flush()
    return user


async def seed(database_url: str, n_patients: int = 12, days: int = 14, reset: bool = False, seed_value: int = 42) -> None:
    db.setup(database_url)
    db.init_db()
    if reset:
        db.truncate_all()
        print("[seed] tables truncated")

    rng = random.Random(seed_value)
    s = db.AsyncSessionLocal()
    try:
        # Идемпотентность: существующие учётки не пересоздаются и не теряют
        # TOTP/пароль. `--reset` (ручной, разрушающий) идёт другим путём.
        admin = await _ensure_user(
            s, "admin@compass.demo", fio="Администратор системы", role="ADMIN",
            totp_secret=DEMO_TOTP or security.generate_totp_secret(),
        )
        doc = await _ensure_user(
            s, "doc@compass.demo", fio="Врач психиатр (демо)", role="CLINICIAN",
            totp_secret=DEMO_TOTP or security.generate_totp_secret(),
        )
        # FR-37: третий врач для демонстрации изоляции roster. Свои пациенты
        # (p1–p2) не видны первому врачу, а пациенты первого — не видны ему.
        doc2 = await _ensure_user(
            s, "doc2@compass.demo", fio="Второй врач (демо)", role="CLINICIAN",
            # Фиксированный TOTP (base32) — воспроизводимый вход на демо/тестах.
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        await s.commit()
        doc_id = doc.id
        doc2_id = doc2.id
        print(f"[seed] admin={admin.email} clinician={doc.email} (TOTP: {doc.totp_secret})")

        await _seed_drugs_async(s)

        elena_pid = None
        profiles = (PATIENT_PROFILES * ((n_patients // len(PATIENT_PROFILES)) + 1))[:n_patients]
        for idx, profile in enumerate(profiles, start=1):
            fio, code, base, slope, noise, _ = profile
            email = f"p{idx}@compass.demo"
            existing = await s.scalar(select(User).where(User.email == email))
            if existing is not None:
                # Уже засеян: историю скринингов/дневник не дублируем и ничего
                # не перезаписываем — сид переиспользуется, а не пересоздаёт.
                if fio == "Елена В.":
                    elena_pid = existing.id
                owner_id = doc2_id if idx <= 2 else doc_id
                await s.execute(
                    text("INSERT INTO clinician_roster(clinician_id, patient_id) VALUES (:cid, :pid) ON CONFLICT DO NOTHING"),
                    {"cid": owner_id, "pid": existing.id},
                )
                print(f"[seed] patient {email} уже есть — пропуск (история сохранена)")
                continue
            patient = User(
                email=email, fio=fio, password_hash=security.hash_password("Demo-Pass-1"),
                role="PATIENT",
            )
            s.add(patient)
            await s.flush()
            pid = patient.id
            if fio == "Елена В.":
                elena_pid = pid
            s.add(Consent(user_id=pid, version=1, text_hash="consent-v1",
                         signed_at=datetime.now(timezone.utc) - timedelta(days=days)))
            s.add(Prescription(patient_id=pid, scale_code=code))
            # p1–p2 закреплены за doc2; остальные — за первым врачом.
            owner_id = doc2_id if idx <= 2 else doc_id
            await s.execute(
                text("INSERT INTO clinician_roster(clinician_id, patient_id) VALUES (:cid, :pid) ON CONFLICT DO NOTHING"),
                {"cid": owner_id, "pid": pid},
            )
            await s.flush()  # чтобы prescribed_scales() видел назначение в цикле скринингов ниже

            totals: list[int] = []
            for day in range(1, days + 1):
                total = base + slope * day + rng.gauss(0, noise)
                total = int(max(1, min(27, round(total))))
                totals.append(total)
                run = await screening_service.start_screening(s, pid)
                answers = _answers_for(profile, rng, total, day)
                await screening_service.add_answers(s, run.id, pid, answers)
                assessment = await screening_service.finish_screening(s, run.id, pid)
                # backdate на (days - day) дней назад: согласованные time-метки всей цепочки
                offset = days - day
                if offset:
                    # F10: параметризованный SQL (без f-string интерполяции значений).
                    delta = timedelta(days=offset)
                    for table, col in (("runs", "started_at"), ("runs", "finished_at"), ("assessments", "computed_at")):
                        await s.execute(
                            text(f"UPDATE {table} SET {col} = {col} - :delta WHERE id = :rid"),
                            {"delta": delta, "rid": run.id},
                        )
                    await s.execute(
                        text("UPDATE answers SET answered_at = answered_at - :delta WHERE run_id = :rid"),
                        {"delta": delta, "rid": run.id},
                    )
            # Дневник: 3 записи по настроению из траектории (created_at — в прошлые дни)
            for day in (3, 8, days - 1):
                # 5-точечная шкала 0..4 с эмодзи (docs/16 §1): 2 — середина.
                mood = max(0, min(4, round(2 + (totals[-1] - totals[0]) / 8 + rng.uniform(-0.75, 0.75))))
                s.add(DiaryEntry(
                    patient_id=pid,
                    content=f"День {day} демо-записи{' — стало сложнее' if mood < 2 else ('' if mood < 3 else ' — заметно лучше')}",
                    mood=mood, sleep_h=round(rng.uniform(5, 8), 1), tags=["демо"],
                    created_at=datetime.now(timezone.utc) - timedelta(days=days - day),
                ))
            print(f"[seed] patient {email} ({fio}) scale={code} 1..{days}d: {totals[0]}→{totals[-1]} triage-last={assessment['triage']}")

        # FR-37: демо-анамнез «Елены В.» по карточке — 34 года, после COVID
        # тревожность/утомляемость/нарушение сна/внутреннее напряжение/страх
        # ухудшения здоровья; лёгкая/средняя выраженность, без тяжёлой депрессии.
        elena_anamnesis = (
            await s.scalar(select(PatientAnamnesis).where(PatientAnamnesis.patient_id == elena_pid))
            if elena_pid is not None else None
        )
        if elena_pid is not None and elena_anamnesis is None:
            s.add(PatientAnamnesis(
                patient_id=elena_pid,
                brief=(
                    "34 года. После перенесённого COVID-19 сохраняются тревожность, "
                    "утомляемость, нарушение сна, ощущение внутреннего напряжения, "
                    "страх ухудшения здоровья. Симптомы лёгкой/средней выраженности, "
                    "тяжёлой депрессии нет."
                ),
                text=(
                    "Жалобы\n"
                    "- Тревожность, ощущение внутреннего напряжения.\n"
                    "- Утомляемость, снижение работоспособности.\n"
                    "- Нарушение сна.\n"
                    "- Страх ухудшения здоровья.\n\n"
                    "Анамнез заболевания\n"
                    "- Симптомы сохраняются после перенесённой коронавирусной инфекции (COVID-19).\n"
                    "- Выраженность лёгкая/средняя; признаков тяжёлой депрессии нет.\n\n"
                    "Анамнез жизни\n"
                    "- 34 года.\n"
                    "- Перенесённая COVID-19.\n"
                    "- Иные сведения в карточке отсутствуют.\n\n"
                    "Текущий статус\n"
                    "- Тревожно-напряжённое состояние, утомляемость, нарушения сна.\n"
                    "- Тяжёлая депрессивная симптоматика не выявлена."
                ),
                created_by=doc_id,
                updated_by=doc_id,
            ))
        await s.commit()
    finally:
        await s.close()
    await db.engine.dispose()

    print("[seed] done. Учётные данные демо:")
    print("  admin: admin@compass.demo / Demo-Pass-1 (2FA: см. выше)")
    print("  doc:   doc@compass.demo / Demo-Pass-1 (2FA: см. выше)")
    print("  doc2:  doc2@compass.demo / Demo-Pass-1 (2FA: JBSWY3DPEHPK3PXP; roster: p1–p2)")
    print("  пациенты: p1..pN@compass.demo / Demo-Pass-1")


def main() -> None:
    from app.config import settings

    parser = argparse.ArgumentParser(description="Генератор демо-данных Clinical Compass")
    parser.add_argument("--url", default=settings.database_url)
    parser.add_argument("--patients", type=int, default=12)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    asyncio.run(seed(args.url, args.patients, args.days, args.reset))


if __name__ == "__main__":
    main()