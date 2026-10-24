"""Приверженность лечению по назначениям (FR-27).

Процент считается от ОЖИДАЕМЫХ приёмов, а не от числа отметок: для каждого
назначения с заданным режимом (times_per_day) знаменатель —
`times_per_day × число дней окна, когда назначение действовало`
(started_on..min(ended_on, today), по московским датам). Числитель —
score = taken + сумма долей partial по `partial_kind` (1/4=0.25, 1/2=0.5,
3/4=0.75, 1 из 2=0.5, 2 из 3≈0.667; less_than_prescribed/self_changed — 0.5,
как раньше).

Общий процент — weighted: сумма score / сумма expected по назначениям
с расписанием, никогда не среднее процентов по препаратам.

Отметки `not_scheduled` («сегодня не назначено»: перерыв, приём по
требованию) не входят ни в числитель, ни в знаменатель процента — это не
пропуск; они показываются отдельным счётчиком.

Отметки без medication_id (исторические — backfill не делался) и отметки на
назначения без режима/вне окна попадают в «вне расчёта» и в знаменатель
не входят. При expected == 0 процент не считается (None).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import AdherenceRecord, PatientMedication

MOSCOW = ZoneInfo("Europe/Moscow")

# Доля принятого приёма для уточнения «частично». Коды — домен CHECK
# adherence_records_partial_kind; NULL у partial быть не может (констрейнт
# required), но `.get` оставляет защиту чтения старых/ручных строк.
PARTIAL_SHARE: dict[str, float] = {
    "quarter": 0.25,
    "half": 0.5,
    "three_quarters": 0.75,
    "one_of_two": 0.5,
    "two_of_three": 2 / 3,
    "less_than_prescribed": 0.5,
    "self_changed": 0.5,
}

# Старые клиенты присылали partial без уточнения. Прежняя семантика «частично»
# = принял меньше назначенного, поэтому им проставляется этот код (0.5).
LEGACY_PARTIAL_KIND = "less_than_prescribed"


def _today() -> date:
    return datetime.now(MOSCOW).date()


def _window(days: int) -> tuple[date, date]:
    """Окно из `days` календарных дней включительно, заканчивается сегодня."""
    end = _today()
    return end - timedelta(days=days - 1), end


def _window_start_dt(start: date) -> datetime:
    return datetime.combine(start, time.min, tzinfo=MOSCOW)


def adherence_dict(r: AdherenceRecord) -> dict:
    return {
        "id": str(r.id),
        "medication_id": str(r.medication_id) if r.medication_id else None,
        "medication": r.medication,
        "status": r.status,
        "partial_kind": r.partial_kind,
        "note": r.note,
        "created_at": r.created_at.isoformat(),
    }


def _partial_score(rows: list[AdherenceRecord]) -> float:
    """Вклад partial-отметок: доля приёма по уточнению, 0.5 при неизвестном коде."""
    return sum(PARTIAL_SHARE.get(r.partial_kind, 0.5) for r in rows if r.status == "partial")


def _expected_days(med: PatientMedication, start: date, end: date) -> int:
    """Дни окна, когда назначение действовало.

    Если дата начала не указана (назначение врача без стартовой даты, EMIAS без
    authoredOn), берём дату создания записи по МСК: иначе назначение, заведённое
    сегодня, требовало бы приёмов за весь период окна и занижало процент.
    """
    if med.started_on is not None:
        first_date = med.started_on
    elif med.created_at is not None:
        first_date = med.created_at.astimezone(MOSCOW).date()
    else:
        first_date = start
    first = max(first_date, start)
    last = min(med.ended_on, end) if med.ended_on else end
    return max(0, (last - first).days + 1)


def _pct(score: float, expected: int) -> int | None:
    return round(score / expected * 100) if expected > 0 else None


async def compute(db: AsyncSession, patient_id, days: int = 14) -> dict:
    """Единый агрегат для витрины пациента и карточки врача (roster/consent — у вызывающих)."""
    start, end = _window(days)
    since = _window_start_dt(start)
    rows = (await db.scalars(
        select(AdherenceRecord)
        .where(AdherenceRecord.patient_id == patient_id, AdherenceRecord.created_at >= since)
        .order_by(AdherenceRecord.created_at.desc())
    )).all()

    meds = (await db.scalars(
        select(PatientMedication).where(PatientMedication.patient_id == patient_id)
    )).all()

    # В знаменатель — назначения с режимом, действовавшие хотя бы день в окне.
    # Статус не фильтруем: остановленное назначение даёт ожидаемые приёмы только
    # до ended_on, остановка его и проставляет.
    included: list[dict] = []
    med_by_id = {}
    for med in meds:
        active_days = _expected_days(med, start, end)
        if med.times_per_day is None or active_days <= 0:
            continue
        own = [r for r in rows if r.medication_id == med.id]
        taken = sum(1 for r in own if r.status == "taken")
        partial = sum(1 for r in own if r.status == "partial")
        missed = sum(1 for r in own if r.status == "missed")
        # «Не назначено сегодня» — не приём и не пропуск: снимает ожидаемые
        # приёмы этого дня с назначения (перерыв/приём по требованию), поэтому
        # вычитается и из числителя, и из знаменателя.
        not_scheduled = sum(1 for r in own if r.status == "not_scheduled")
        expected = max(0, med.times_per_day * active_days - not_scheduled * med.times_per_day)
        score = taken + _partial_score(own)
        med_by_id[med.id] = med
        included.append({
            "id": str(med.id),
            "title": med.title,
            "status": med.status,
            "times_per_day": med.times_per_day,
            "started_on": med.started_on.isoformat() if med.started_on else None,
            "ended_on": med.ended_on.isoformat() if med.ended_on else None,
            "active_days": active_days,
            "expected": expected,
            "taken": taken,
            "partial": partial,
            "missed": missed,
            "not_scheduled": not_scheduled,
            "score": score,
            "pct": _pct(score, expected),
        })

    included_ids = set(med_by_id)
    excluded = [
        r for r in rows
        if r.medication_id is None or r.medication_id not in included_ids
    ]

    score = sum(m["score"] for m in included)
    expected = sum(m["expected"] for m in included)
    taken = sum(1 for r in rows if r.status == "taken")
    partial = sum(1 for r in rows if r.status == "partial")
    missed = sum(1 for r in rows if r.status == "missed")
    not_scheduled = sum(1 for r in rows if r.status == "not_scheduled")

    active_meds = (await db.scalars(
        select(PatientMedication)
        .where(PatientMedication.patient_id == patient_id, PatientMedication.status == "active")
        .order_by(PatientMedication.created_at.desc())
    )).all()

    return {
        "days": days,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "summary": {"pct": _pct(score, expected), "score": score, "expected": expected},
        "medications": included,
        "active_medications": [
            {
                "id": str(m.id),
                "title": m.title,
                "times_per_day": m.times_per_day,
                "started_on": m.started_on.isoformat() if m.started_on else None,
            }
            for m in active_meds
        ],
        "total": len(rows),
        "taken": taken,
        "partial": partial,
        "missed": missed,
        "not_scheduled": not_scheduled,
        "excluded": {"count": len(excluded), "items": [adherence_dict(r) for r in excluded]},
        "items": [adherence_dict(r) for r in rows],
        "today": today_record(rows),
    }


def summary_payload(data: dict) -> dict:
    """Форма агрегата для карточки врача (report.adherence и /clinician/.../adherence)."""
    return {
        "days": data["days"],
        "window": data["window"],
        "pct": data["summary"]["pct"],
        "score": data["summary"]["score"],
        "expected": data["summary"]["expected"],
        # Счётчики отметок за окно (все записи) — для сравнения с ожидаемым числом.
        "total": data["total"],
        "taken": data["taken"],
        "partial": data["partial"],
        "missed": data["missed"],
        "not_scheduled": data["not_scheduled"],
        "medications": data["medications"],
        "excluded_count": data["excluded"]["count"],
        "records": data["items"][:10],
    }


def _normalize_partial_kind(
    status: str, partial_kind: str | None, legacy_partial: bool,
) -> str | None:
    """Инвариант kind ↔ status: kind только для partial и обязателен для него.

    `legacy_partial` — старый клиент, не приславший поле вовсе: принимаем и
    трактуем прежнюю семантику (принял меньше назначенного, 0.5). Явный null
    в новом контракте — ошибка клиента, 422 PARTIAL_KIND_REQUIRED.
    """
    if status != "partial":
        if partial_kind is not None:
            raise Problem(
                422, "PARTIAL_KIND_INVALID",
                "partial_kind допустим только при status=partial",
            )
        return None
    if partial_kind is None:
        if legacy_partial:
            return LEGACY_PARTIAL_KIND
        raise Problem(
            422, "PARTIAL_KIND_REQUIRED",
            "Для status=partial укажите partial_kind: quarter | half | three_quarters | "
            "one_of_two | two_of_three | less_than_prescribed | self_changed",
        )
    if partial_kind not in PARTIAL_SHARE:
        raise Problem(
            422, "PARTIAL_KIND_INVALID",
            "Неизвестный partial_kind: quarter | half | three_quarters | one_of_two | "
            "two_of_three | less_than_prescribed | self_changed",
        )
    return partial_kind


async def create_record(
    db: AsyncSession, patient_id, medication_id=None, medication: str | None = None,
    status: str = "taken", note: str | None = None, partial_kind: str | None = None,
    legacy_partial: bool = False,
) -> dict:
    """Отметка пациента. consent-гейт у роутера, владение назначением — здесь."""
    partial_kind = _normalize_partial_kind(status, partial_kind, legacy_partial)
    med = None
    if medication_id is not None:
        med = await db.get(PatientMedication, medication_id)
        if med is None or med.patient_id != patient_id or med.status != "active":
            raise Problem(
                422, "MEDICATION_NOT_ACTIVE",
                "Назначение не найдено или не активно — отметку можно связать "
                "только с активным назначением пациента",
            )
        medication = med.title  # текст названия — для отображения и истории
    rec = AdherenceRecord(
        patient_id=patient_id,
        medication_id=med.id if med is not None else None,
        medication=medication,
        status=status,
        partial_kind=partial_kind,
        note=note,
    )
    db.add(rec)
    await db.commit()
    return adherence_dict(rec)


def today_record(rows: list[AdherenceRecord]) -> dict | None:
    today = _today()
    for r in rows:
        if r.created_at.astimezone(MOSCOW).date() == today:
            return adherence_dict(r)
    return None
