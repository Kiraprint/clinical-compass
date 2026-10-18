"""Симулятор демо-данных (FR-14): детерминированный генератор пациентов.

Реалистичная динамика: базовый уровень + тренд + шум; 1–2 пациента с RED-траекторией
(суицидальный пункт / высокие баллы) для демонстрации эскалаций.
Запуск: python -m app.seed --reset --patients 12 --days 14
"""

from __future__ import annotations

import argparse
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import db, security
from app.models import Answer, Assessment, Consent, DiaryEntry, Prescription, Run, User
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


def seed(database_url: str, n_patients: int = 12, days: int = 14, reset: bool = False, seed_value: int = 42) -> None:
    db.setup(database_url)
    db.init_db()
    if reset:
        db.truncate_all()
        print("[seed] tables truncated")

    rng = random.Random(seed_value)
    s = db.SessionLocal()
    try:
        admin = User(
            email="admin@compass.demo", fio="Администратор системы",
            password_hash=security.hash_password("Demo-Pass-1"), role="ADMIN",
            totp_secret=DEMO_TOTP or security.generate_totp_secret(),
        )
        doc = User(
            email="doc@compass.demo", fio="Врач психиатр (демо)",
            password_hash=security.hash_password("Demo-Pass-1"), role="CLINICIAN",
            totp_secret=DEMO_TOTP or security.generate_totp_secret(),
        )
        s.add_all([admin, doc])
        s.flush()
        doc_id = doc.id
        s.commit()
        print(f"[seed] admin={admin.email} clinician={doc.email} (TOTP: {doc.totp_secret})")

        profiles = (PATIENT_PROFILES * ((n_patients // len(PATIENT_PROFILES)) + 1))[:n_patients]
        for idx, profile in enumerate(profiles, start=1):
            fio, code, base, slope, noise, _ = profile
            email = f"p{idx}@compass.demo"
            patient = User(
                email=email, fio=fio, password_hash=security.hash_password("Demo-Pass-1"),
                role="PATIENT",
            )
            s.add(patient)
            s.flush()
            pid = patient.id
            s.add(Consent(user_id=pid, version=1, text_hash="consent-v1",
                         signed_at=datetime.now(timezone.utc) - timedelta(days=days)))
            s.add(Prescription(patient_id=pid, scale_code=code))
            s.execute(
                text(f"INSERT INTO clinician_roster(clinician_id, patient_id) VALUES ('{doc_id}', '{pid}') ON CONFLICT DO NOTHING")
            )
            s.flush()  # чтобы prescribed_scales() видел назначение в цикле скринингов ниже

            totals: list[int] = []
            for day in range(1, days + 1):
                total = base + slope * day + rng.gauss(0, noise)
                total = int(max(1, min(27, round(total))))
                totals.append(total)
                run = screening_service.start_screening(s, pid)
                answers = _answers_for(profile, rng, total, day)
                screening_service.add_answers(s, run.id, pid, answers)
                assessment = screening_service.finish_screening(s, run.id, pid)
                # backdate на (days - day) дней назад: согласованные time-метки всей цепочки
                offset = days - day
                if offset:
                    for table, col in (("runs", "started_at"), ("runs", "finished_at"), ("assessments", "computed_at")):
                        s.execute(text(f"UPDATE {table} SET {col} = {col} - interval '{offset} days' WHERE id = '{run.id}'"))
                    s.execute(text(
                        f"UPDATE answers SET answered_at = answered_at - interval '{offset} days' WHERE run_id = '{run.id}'"
                    ))
            # Дневник: 3 записи по настроению из траектории (created_at — в прошлые дни)
            for day in (3, 8, days - 1):
                mood = max(1, min(10, round(5 + (totals[-1] - totals[0]) / 4 + rng.uniform(-1.5, 1.5))))
                s.add(DiaryEntry(
                    patient_id=pid,
                    content=f"День {day} демо-записи{' — стало сложнее' if mood < 4 else ('' if mood < 7 else ' — заметно лучше')}",
                    mood=mood, sleep_h=round(rng.uniform(5, 8), 1), tags=["демо"],
                    created_at=datetime.now(timezone.utc) - timedelta(days=days - day),
                ))
            print(f"[seed] patient {email} ({fio}) scale={code} 1..{days}d: {totals[0]}→{totals[-1]} triage-last={assessment['triage']}")
        s.commit()
    finally:
        s.close()

    print("[seed] done. Учётные данные демо:")
    print("  admin: admin@compass.demo / Demo-Pass-1 (2FA: см. выше)")
    print("  doc:   doc@compass.demo / Demo-Pass-1 (2FA: см. выше)")
    print("  пациенты: p1..pN@compass.demo / Demo-Pass-1")


def main() -> None:
    from app.config import settings

    parser = argparse.ArgumentParser(description="Генератор демо-данных Clinical Compass")
    parser.add_argument("--url", default=settings.database_url)
    parser.add_argument("--patients", type=int, default=12)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    seed(args.url, args.patients, args.days, args.reset)


if __name__ == "__main__":
    main()