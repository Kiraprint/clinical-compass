#!/usr/bin/env python3
"""Сбор источников бенчмарка (воспроизводимо, при сборке — не в git).

Тянет из открытых источников:
- абстракты 8 мета-анализов/систематических обзоров из PMC (E-utilities, open access);
- факт-листы ВОЗ «Depression» (EN) и «Anxiety disorders» (EN + RU).

Почему не коммитим сырьё: сторонний контент (PMC/ВОЗ, лицензии CC BY-NC-SA и др.)
не заливаем в репозиторий — та же политика, что в docs/08 §2 (корпус собирается
при индексации, в git хранится только наш код). Скрипт детерминирован по ID/URL.

Запуск: bench/.venv/bin/python bench/fetch_sources.py
Результат: bench/sources/pmc_meta.json, bench/sources/who.json (в .gitignore).
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sources")
os.makedirs(OUT, exist_ok=True)

# Мета-анализы/систематические обзоры по тревоге/депрессии (PMC OA).
PMC_IDS = [
    "10277900",  # MCT vs CBT при генерализованном тревожном расстройстве
    "9790363",   # remote CBT при ГАД
    "13571187",  # dCBT при сочетанной хронической боли, тревоге и депрессии
    "13641322",  # австралийские цифровые службы психического здоровья
    "13653422",  # куркумин: депрессия/тревога/стресс (GRADE)
    "13655782",  # цифровые интервенции и качество жизни при онкологии
    "13660419",  # тай-чи: среда упражнений при депрессии/тревоге
    "13264932",  # аэробные упражнения при большом депрессивном расстройстве
]

WHO_URLS = {
    "depression_en": "https://www.who.int/news-room/fact-sheets/detail/depression",
    "anxiety_en": "https://www.who.int/news-room/fact-sheets/detail/anxiety-disorders",
    "anxiety_ru": "https://www.who.int/ru/news-room/fact-sheets/detail/anxiety-disorders",
}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def fetch_pmc() -> list[dict]:
    out = []
    for pid in PMC_IDS:
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(
            {"db": "pmc", "id": pid, "rettype": "xml", "retmode": "xml"}
        )
        xml = _get(url)
        t = re.findall(r"<article-title>(.*?)</article-title>", xml, re.S)
        ab = re.findall(r"<abstract[^>]*>(.*?)</abstract>", xml, re.S)
        out.append(
            {
                "pmcid": pid,
                "title": _clean(t[0]) if t else "",
                "abstract": _clean(ab[0]) if ab else "",
            }
        )
        print(f"PMC {pid}: {len(out[-1]['abstract'])} chars")
    return out


def _who_body(html: str) -> list[str]:
    m = re.search(r'<div class="sf-item-header-wrapper"', html)
    seg = html[m.start():] if m else html
    seg = re.split(r'<footer|class="sf-item-toolbar-wrapper"|class="secondary-container"', seg)[0]
    seg = re.sub(r"<script.*?</script>", "", seg, flags=re.S)
    seg = re.sub(r"<(p|h1|h2|h3|li|ul|hr|br)[^>]*>", "\n", seg)
    seg = re.sub(r"<[^>]+>", " ", seg)
    import html as _html

    seg = _html.unescape(seg)
    lines = [re.sub(r"\s+", " ", x).strip() for x in seg.split("\n")]
    return [x for x in lines if x]


def fetch_who() -> dict:
    out = {}
    for key, url in WHO_URLS.items():
        out[key] = _who_body(_get(url))
        print(f"WHO {key}: {len(out[key])} lines")
    return out


def main() -> None:
    pmc = fetch_pmc()
    who = fetch_who()
    json.dump(pmc, open(os.path.join(OUT, "pmc_meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(who, open(os.path.join(OUT, "who.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"Сохранено: {len(pmc)} PMC, {len(who)} WHO → {OUT}")


if __name__ == "__main__":
    main()
