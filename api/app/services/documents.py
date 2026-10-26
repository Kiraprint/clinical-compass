"""FR-40 (docs/18 §5, §7): СЭМД-документ протокола — CDA R2 XML и печатный PDF.

Документ собирается **только** из подтверждённого протокола (`status` в
`approved|signed`): разделы берутся из `session_protocols.sections`, поэтому ИИ
здесь ни при чём — подписывается ровно то, что врач подтвердил.

ПДн в документ попадают по минимуму и никогда не выдумываются:

- пациент: ФИО и дата рождения из карточки; СНИЛС не выводится — в БД он
  хранится только как sha256 (`emias_links.snils_hash`), восстановить нельзя;
- автор: ФИО врача из `users.fio`, должность — `CC_SIGNING_DOCTOR_POSITION`;
- МО (custodian): `CC_SIGNING_MO_ORG`.

Экранирование текста делают `xml.etree` (CDA) и `html.escape` (шаблон PDF):
протокол — свободный текст, в нём встречаются `<`, `&`, кавычки. Содержимое
нигде не логируется (docs/18 §2).
"""

from __future__ import annotations

import html
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

CDA_NS = "urn:hl7-org:v3"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", CDA_NS)
ET.register_namespace("xsi", XSI_NS)

# Кодировки из HL7: LOINC для узнаваемых разделов, МКБ-10 — международный OID.
LOINC = "2.16.840.1.113883.6.1"
ICD10 = "2.16.840.1.113883.6.3"
CONFIDENTIALITY = "2.16.840.1.113883.5.25"

CHANNELS = {"in_person": "очный", "online": "онлайн", "phone": "телефонный"}

MENTAL_TITLES = (
    ("appearance_behavior", "Внешний вид и поведение"),
    ("consciousness_orientation", "Сознание и ориентированность"),
    ("perception", "Восприятие"),
    ("thinking_speech", "Мышление и речь"),
    ("emotions_volition", "Эмоции и воля"),
    ("criticism", "Критика"),
    ("suicidal_risk", "Суицидальный риск"),
)

TREATMENT_FIELDS = ("drug", "dose", "schedule", "duration")
TREATMENT_COLUMNS = ("Препарат", "Доза", "Схема", "Длительность")


@dataclass(frozen=True)
class Patient:
    id: str
    fio: str | None
    birth_date: object | None  # date | None — как пришло из карточки


@dataclass(frozen=True)
class Clinician:
    id: str
    fio: str
    position: str


@dataclass(frozen=True)
class Stamp:
    """Визуальная отметка о подписи (п. 9 приказа 209н: «ПОДПИСАНО»)."""

    role: str
    fio: str
    position: str
    snils: str | None
    issuer: str
    serial: str
    not_before: datetime
    not_after: datetime
    thumbprint: str
    signed_at: datetime


def _ts(moment: datetime) -> str:
    """CDA TS: YYYYMMDDHHMMSS±hhmm (без микросекунд, зона обязательна)."""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    offset = aware.utcoffset() or timezone.utc.utcoffset(aware)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return aware.strftime("%Y%m%d%H%M%S") + f"{sign}{hours:02d}{minutes:02d}"


def _tag(name: str) -> str:
    return f"{{{CDA_NS}}}{name}"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, _tag(tag), dict(attrs))
    if text:
        node.text = text
    return node


def _person_name(parent: ET.Element, fio: str) -> None:
    """ФИО одной строкой: дробление на family/given задним числом — выдумка."""
    _sub(_sub(parent, "name"), "given", fio)


def _local_section(body: ET.Element, title: str) -> ET.Element:
    """Секция без кода из справочника: OTH + исходное название (не выдумываем OID)."""
    section = _sub(_sub(body, "component"), "section")
    _sub(_sub(section, "code", nullFlavor="OTH"), "originalText", title)
    _sub(section, "title", title)
    return section


def _text_section(body: ET.Element, title: str, text: str, code: str | None = None) -> None:
    component = _sub(body, "component")
    section = _sub(component, "section")
    if code:
        _sub(section, "code", code=code, codeSystem=LOINC, displayName=title)
    else:
        _sub(_sub(section, "code", nullFlavor="OTH"), "originalText", title)
    _sub(section, "title", title)
    text_node = _sub(section, "text")
    for line in text.splitlines() or [""]:
        _sub(text_node, "paragraph", line)


def render_cda(
    *,
    document_id: str,
    version: int,
    title: str,
    patient: Patient,
    clinician: Clinician,
    organization: str,
    effective_at: datetime,
    sections: dict,
) -> bytes:
    """СЭМД: CDA R2 XML протокола приёма (UTF-8, декларация XML)."""
    root = ET.Element(_tag("ClinicalDocument"))
    _sub(root, "realmCode", code="RU")
    # typeId CDA R2 обязателен; templateId конкретного вида СЭМД задаёт профиль
    # передачи в РЭМД (PR P5) — здесь его нет, чтобы не выдавать чужой OID.
    _sub(root, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_MT000040")
    _sub(root, "id", root=f"urn:uuid:{document_id}", extension=str(version))
    _sub(root, "code", code="34133-9", codeSystem=LOINC, displayName="Протокол приёма")
    _sub(root, "title", title)
    _sub(root, "effectiveTime", value=_ts(effective_at))
    _sub(root, "confidentialityCode", code="R", codeSystem=CONFIDENTIALITY)
    _sub(root, "languageCode", code="ru-RU")

    role = _sub(_sub(root, "recordTarget"), "patientRole")
    _sub(role, "id", root=f"urn:uuid:{patient.id}")
    patient_node = _sub(role, "patient")
    if patient.fio:
        _person_name(patient_node, patient.fio)
    if patient.birth_date is not None:
        birth = patient.birth_date
        value = birth.strftime("%Y%m%d") if hasattr(birth, "strftime") else str(birth)
        _sub(patient_node, "birthTime", value=value)

    author = _sub(root, "author")
    _sub(author, "time", value=_ts(effective_at))
    assigned = _sub(author, "assignedAuthor")
    _sub(assigned, "id", root=f"urn:uuid:{clinician.id}")
    _person_name(_sub(assigned, "assignedPerson"), clinician.fio)
    _sub(_sub(assigned, "representedOrganization"), "name", organization)

    custodian = _sub(root, "custodian")
    organization_node = _sub(_sub(custodian, "assignedCustodian"), "representedCustodianOrganization")
    _sub(organization_node, "name", organization)

    body = _sub(_sub(root, "component"), "structuredBody")

    passport = sections.get("passport") or {}
    if passport:
        section = _local_section(body, "Паспортная часть приёма")
        text_node = _sub(section, "text")
        if passport.get("visit_at"):
            _sub(text_node, "paragraph", f"Дата и время приёма: {passport['visit_at']}")
        if passport.get("channel"):
            channel = CHANNELS.get(passport["channel"], passport["channel"])
            _sub(text_node, "paragraph", f"Канал: {channel}")
        if passport.get("legal_basis"):
            _sub(text_node, "paragraph", f"Основание: {passport['legal_basis']}")

    for key, heading, loinc in (
        ("complaints", "Жалобы", "10154-3"),
        ("anamnesis_morbi", "Анамнез заболевания", "10164-2"),
    ):
        value = (sections.get(key) or "").strip()
        if value:
            _text_section(body, heading, value, code=loinc)

    mental = sections.get("mental_status") or {}
    mental_filled = [
        (heading, (mental.get(key) or "").strip())
        for key, heading in MENTAL_TITLES
        if (mental.get(key) or "").strip()
    ]
    if mental_filled:
        section = _local_section(body, "Психический статус")
        text_node = _sub(section, "text")
        for heading, value in mental_filled:
            _sub(text_node, "paragraph", f"{heading}: {value}")
        # Подразделы §5.1 — вложенными секциями: так их читает профиль РЭМД.
        for heading, value in mental_filled:
            nested = _sub(_sub(section, "component"), "section")
            _sub(_sub(nested, "code", nullFlavor="OTH"), "originalText", heading)
            _sub(nested, "title", heading)
            _sub(_sub(nested, "text"), "paragraph", value)

    somatic = (sections.get("somatic_status") or "").strip()
    if somatic:
        _text_section(body, "Соматический статус", somatic)

    diagnosis = sections.get("diagnosis") or {}
    if diagnosis:
        section = _local_section(body, "Диагноз")
        text_node = _sub(section, "text")
        if diagnosis.get("syndrome"):
            _sub(text_node, "paragraph", f"Синдром: {diagnosis['syndrome']}")
        code = (diagnosis.get("icd10") or "").strip()
        if code:
            _sub(text_node, "paragraph", f"МКБ-10: {code}")
        if diagnosis.get("rationale"):
            _sub(text_node, "paragraph", f"Обоснование: {diagnosis['rationale']}")
        if code:
            observation = _sub(
                _sub(_sub(section, "entry"), "observation"),
                "value",
                **{f"{{{XSI_NS}}}type": "CD"},
            )
            observation.set("code", code)
            observation.set("codeSystem", ICD10)
            observation.set("codeSystemName", "МКБ-10")
            observation.set("displayName", code)

    treatment = sections.get("treatment") or []
    if treatment:
        section = _local_section(body, "Лечение")
        table = _sub(_sub(section, "text"), "table")
        header = _sub(_sub(table, "thead"), "tr")
        for column in TREATMENT_COLUMNS:
            _sub(_sub(header, "th"), "paragraph", column)
        tbody = _sub(table, "tbody")
        for item in treatment:
            row = _sub(tbody, "tr")
            for field in TREATMENT_FIELDS:
                _sub(_sub(row, "td"), "paragraph", str(item.get(field) or ""))

    recommendations = (sections.get("recommendations") or "").strip()
    if recommendations:
        _text_section(body, "Рекомендации", recommendations)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _stamp_block(stamp: Stamp) -> str:
    role = "Врач" if stamp.role == "clinician" else "Уполномоченное лицо МО"
    snils = _escape(stamp.snils) if stamp.snils else "не указан"
    return (
        '<section class="stamp">'
        '<div class="stamp-head">ПОДПИСАНО</div>'
        f'<div class="stamp-role">{_escape(role)}</div>'
        '<table class="stamp-table">'
        f"<tr><th>ФИО</th><td>{_escape(stamp.fio)}</td></tr>"
        f"<tr><th>Должность</th><td>{_escape(stamp.position)}</td></tr>"
        f"<tr><th>СНИЛС</th><td>{snils}</td></tr>"
        f"<tr><th>Издатель сертификата</th><td>{_escape(stamp.issuer)}</td></tr>"
        f"<tr><th>Серийный номер</th><td>{_escape(stamp.serial)}</td></tr>"
        f"<tr><th>Срок действия</th><td>{stamp.not_before:%d.%m.%Y} — "
        f"{stamp.not_after:%d.%m.%Y}</td></tr>"
        f"<tr><th>Отпечаток</th><td><code>{_escape(stamp.thumbprint[:16])}</code></td></tr>"
        f"<tr><th>Дата и время подписи</th><td>{stamp.signed_at:%d.%m.%Y %H:%M:%S}</td></tr>"
        "</table></section>"
    )


_CSS = """
@page {
  size: A4;
  margin: 20mm 18mm 22mm 18mm;
  @bottom-center { content: "Страница " counter(page) " из " counter(pages);
                   font-size: 8pt; color: #555; }
  @bottom-right { content: "%(footer)s"; font-size: 8pt; color: #555; }
}
body { font-family: "DejaVu Sans", "Liberation Sans", sans-serif; font-size: 10.5pt;
       line-height: 1.45; color: #111; }
h1 { font-size: 15pt; margin: 0 0 2mm 0; }
h2 { font-size: 11.5pt; margin: 5mm 0 1.5mm 0; border-bottom: 0.4pt solid #999; }
.meta { font-size: 9pt; color: #444; margin-bottom: 4mm; }
.kv { width: 100%%; border-collapse: collapse; font-size: 9.5pt; }
.kv th { text-align: left; width: 45mm; font-weight: 600; padding: 0.7mm 2mm 0.7mm 0; }
.mental { margin: 0; }
.mental dt { font-weight: 600; }
.mental dd { margin: 0 0 1.5mm 4mm; }
table.treatment { width: 100%%; border-collapse: collapse; font-size: 9.5pt; }
table.treatment th, table.treatment td { border: 0.4pt solid #888; padding: 1mm 1.5mm;
                                         vertical-align: top; text-align: left; }
.stamps { page-break-inside: avoid; margin-top: 6mm; }
.stamp { border: 0.8pt solid #333; padding: 3mm; margin-bottom: 4mm;
         page-break-inside: avoid; }
.stamp-head { font-weight: 700; letter-spacing: 1.5pt; font-size: 11pt; }
.stamp-role { font-size: 9pt; color: #444; margin-bottom: 1.5mm; }
.stamp-table { width: 100%%; border-collapse: collapse; font-size: 9pt; }
.stamp-table th { text-align: left; width: 55mm; font-weight: 600; padding: 0.4mm 2mm 0.4mm 0; }
.note { font-size: 8pt; color: #555; margin-top: 2mm; }
"""


def render_pdf(
    *,
    document_id: str,
    version: int,
    title: str,
    organization: str,
    effective_at: datetime,
    patient: Patient,
    clinician: Clinician,
    sections: dict,
    stamps: list[Stamp],
) -> bytes:
    """Печатная форма подписанного протокола (WeasyPrint: HTML/CSS → PDF)."""
    parts = [
        f"<h1>{_escape(title)}</h1>",
        '<div class="meta">'
        f"{_escape(organization)} · документ {_escape(document_id)} · "
        f"версия {int(version)} · дата {effective_at:%d.%m.%Y}"
        "</div>",
    ]

    passport = sections.get("passport") or {}
    rows: list[tuple[str, object]] = []
    if passport.get("visit_at"):
        rows.append(("Дата и время приёма", passport["visit_at"]))
    if passport.get("channel"):
        rows.append(("Канал", CHANNELS.get(passport["channel"], passport["channel"])))
    if passport.get("legal_basis"):
        rows.append(("Основание", passport["legal_basis"]))
    if patient.fio:
        rows.append(("Пациент", patient.fio))
    if patient.birth_date is not None:
        birth = patient.birth_date
        value = birth.strftime("%d.%m.%Y") if hasattr(birth, "strftime") else str(birth)
        rows.append(("Дата рождения", value))
    rows.append(("Врач", f"{clinician.fio} ({clinician.position})"))
    cells = "".join(
        f"<tr><th>{_escape(name)}</th><td>{_escape(value)}</td></tr>"
        for name, value in rows
    )
    parts.append(f'<table class="kv">{cells}</table>')

    def paragraph(heading: str, text: str) -> None:
        if not text.strip():
            return
        body = "".join(
            f"<p>{_escape(line)}</p>" for line in text.splitlines() if line.strip()
        )
        parts.append(f"<h2>{_escape(heading)}</h2>{body}")

    paragraph("Жалобы", sections.get("complaints") or "")
    paragraph("Анамнез заболевания", sections.get("anamnesis_morbi") or "")

    mental = sections.get("mental_status") or {}
    mental_rows = [
        (heading, (mental.get(key) or "").strip())
        for key, heading in MENTAL_TITLES
        if (mental.get(key) or "").strip()
    ]
    if mental_rows:
        items = "".join(
            f"<dt>{_escape(heading)}</dt><dd>{_escape(value)}</dd>"
            for heading, value in mental_rows
        )
        parts.append(f'<h2>Психический статус</h2><dl class="mental">{items}</dl>')

    paragraph("Соматический статус", sections.get("somatic_status") or "")

    diagnosis = sections.get("diagnosis") or {}
    diagnosis_lines: list[str] = []
    if diagnosis.get("syndrome"):
        diagnosis_lines.append(f"Синдром: {diagnosis['syndrome']}")
    code = (diagnosis.get("icd10") or "").strip()
    if code:
        diagnosis_lines.append(f"МКБ-10: {code}")
    if diagnosis.get("rationale"):
        diagnosis_lines.append(f"Обоснование: {diagnosis['rationale']}")
    if diagnosis_lines:
        parts.append(
            "<h2>Диагноз</h2>"
            + "".join(f"<p>{_escape(line)}</p>" for line in diagnosis_lines)
        )

    treatment = sections.get("treatment") or []
    if treatment:
        head = "".join(f"<th>{_escape(column)}</th>" for column in TREATMENT_COLUMNS)
        body = "".join(
            "<tr>"
            + "".join(
                f"<td>{_escape(item.get(field) or '')}</td>" for field in TREATMENT_FIELDS
            )
            + "</tr>"
            for item in treatment
        )
        parts.append(
            '<h2>Лечение</h2><table class="treatment">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        )

    paragraph("Рекомендации", sections.get("recommendations") or "")

    stamps_html = "".join(_stamp_block(stamp) for stamp in stamps)
    parts.append(
        '<div class="stamps"><h2>Электронные подписи</h2>'
        f"{stamps_html}"
        '<p class="note">Демонстрационная подпись: УКЭП аккредитованного УЦ не '
        "выдавалась. Документ подписан тестовым УЦ «Compass Demo CA» по ГОСТ Р "
        "34.10-2012 / 34.11-2012 (Стрибог-256); визуальная отметка соответствует "
        "п. 9 приказа Минздрава России № 209н от 25.03.2026.</p></div>"
    )

    footer = _escape(f"Документ {document_id} · версия {int(version)}")
    document = (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{_escape(title)}</title>"
        f"<style>{_CSS % {'footer': footer}}</style></head><body>"
        + "".join(parts)
        + "</body></html>"
    )
    from weasyprint import HTML  # импорт локальный: нужен только для PDF

    buffer = io.BytesIO()
    HTML(string=document, base_url=None).write_pdf(target=buffer)
    return buffer.getvalue()
