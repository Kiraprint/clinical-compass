"""Коннектор ВОЗ (IRIS): лицензия, TEXT bitstream, белый список (оффлайн)."""

from __future__ import annotations

import urllib.parse
import urllib.request

import pytest

from app.enrichment import who
from app.enrichment.config import WHOConfig

from .enrichment_fixtures import WHO_ITEM, WHO_TEXT


def test_whitelist_is_unique_and_iris_only():
    handles = [h.handle for h in who.WHO_HANDLES]
    uuids = [h.uuid for h in who.WHO_HANDLES]
    assert len(handles) == len(set(handles))
    assert len(uuids) == len(set(uuids))
    assert all(h.handle.startswith("10665/") for h in who.WHO_HANDLES)
    assert any(h.handle == "10665/374250" for h in who.WHO_HANDLES)


def test_detect_license_from_document_text():
    assert who.detect_license(WHO_TEXT) == "CC BY-NC-SA 3.0 IGO"
    assert who.detect_license("All rights reserved. No part of this publication may be reproduced.") == "proprietary"
    assert who.detect_license("© World Health Organization 2013") == "proprietary"
    assert who.detect_license("This work is licensed under Creative Commons Attribution 4.0") == "other-cc"
    assert who.detect_license("No licence statement here.") == ""


def test_parse_item_builds_document_with_license_and_revision():
    meta = next(h for h in who.WHO_HANDLES if h.handle == "10665/374250")
    doc = who.parse_iris_item(WHO_ITEM, WHO_TEXT, meta)
    assert doc.kind == "guideline"
    assert doc.external_id == "10665/374250"
    assert doc.guideline_id == "who-10665-374250"
    assert doc.year == 2023
    assert doc.license == "CC BY-NC-SA 3.0 IGO"
    assert doc.url == "https://iris.who.int/handle/10665/374250"
    assert doc.revision.startswith("2023-11-20#")
    assert len(doc.text) > 5000


def test_parse_item_without_license_is_honest():
    meta = next(h for h in who.WHO_HANDLES if h.handle == "10665/374250")
    doc = who.parse_iris_item(WHO_ITEM, "No licence statement here. " * 300, meta)
    assert doc.license == ""


def test_pick_text_stream_prefers_english():
    streams = [
        {"name": "x-jpn.pdf.txt", "_bundle": "TEXT"},
        {"name": "x-eng.pdf.txt", "_bundle": "TEXT"},
        {"name": "x.pdf", "_bundle": "ORIGINAL"},
    ]
    assert who._pick_text_stream(streams)["name"] == "x-eng.pdf.txt"
    assert who._pick_text_stream([{"name": "x.pdf", "_bundle": "ORIGINAL"}]) is None


def test_fetch_documents_skips_failed_handle_and_reports_error(monkeypatch):
    cfg = WHOConfig(
        handles=(
            who.WHO_HANDLES[0],
            who.WHO_HANDLES[1],
        )
    )
    item = dict(WHO_ITEM)
    calls: list[str] = []

    def fake_get_json(cfg, url):
        calls.append(url)
        if url.endswith("/core/items/" + cfg.handles[0].uuid):
            return {
                **item,
                "_links": {"bundles": {"href": "https://iris.example/bundles"}},
            }
        if url.endswith("/bundles"):
            return {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "TEXT",
                            "_links": {
                                "bitstreams": {"href": "https://iris.example/bundle/text"}
                            },
                        }
                    ]
                }
            }
        if "bundle/text" in url:
            return {
                "_embedded": {
                    "bitstreams": [
                        {
                            "name": "9789240084278-eng.pdf.txt",
                            "_links": {"content": {"href": "https://iris.example/content/text"}},
                        }
                    ]
                }
            }
        if url.endswith("/core/items/" + cfg.handles[1].uuid):
            return {
                "handle": cfg.handles[1].handle,
                "metadata": {},
                "_links": {"bundles": {"href": "https://iris.example/bundles-empty"}},
            }
        if url.endswith("/bundles-empty"):
            return {"_embedded": {"bundles": []}}
        raise AssertionError(f"unexpected url {url}")

    def fake_request(cfg, url, *, accept):
        assert "content/text" in url
        return WHO_TEXT.encode()

    monkeypatch.setattr(who, "_get_json", fake_get_json)
    monkeypatch.setattr(who, "_request", fake_request)

    docs, plan, errors = who.fetch_documents(cfg)
    assert len(docs) == 1 and docs[0].external_id == cfg.handles[0].handle
    assert len(errors) == 1 and "нет TEXT bitstream" in errors[0]
    assert plan["handles"] == [cfg.handles[0].handle, cfg.handles[1].handle]


def test_fetch_documents_raises_when_iris_unreachable(monkeypatch):
    def fake_get_json(cfg, url):
        raise who.SourceError("WHO IRIS HTTP 503")

    monkeypatch.setattr(who, "_get_json", fake_get_json)
    docs, plan, errors = who.fetch_documents(WHOConfig(handles=(who.WHO_HANDLES[0],)))
    assert docs == [] and errors and "503" in errors[0]


def test_fetch_documents_rejects_mismatched_handle(monkeypatch):
    """UUID и handle разошлись — индексировать чужой документ нельзя."""
    cfg = WHOConfig(handles=(who.WHO_HANDLES[0],))

    def fake_get_json(cfg, url):
        return {
            **WHO_ITEM,
            "handle": "10665/999999",
            "_links": {"bundles": {"href": "https://iris.example/bundles"}},
        }

    monkeypatch.setattr(who, "_get_json", fake_get_json)
    docs, _, errors = who.fetch_documents(cfg)
    assert docs == []
    assert errors and "10665/999999" in errors[0]


# --- SSRF/редиректы: URL приходят из JSON IRIS, поэтому проверяется каждый хоп ---


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RedirectChainOpener:
    """Подмена urlopen: прогоняет Location через тот же обработчик редиректов."""

    def __init__(self, hops: list[str], body: bytes = b"payload"):
        self.hops = hops
        self.body = body
        self.urls: list[str] = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        handler = who._IRISRedirectHandler()
        current = request
        for hop in self.hops:
            handler.redirect_request(current, None, 302, "Found", {}, hop)
            current = urllib.request.Request(urllib.parse.urljoin(current.full_url, hop))
        return _Response(self.body)


@pytest.mark.parametrize(
    "url",
    [
        "http://iris.who.int/server/api/core/items/x",  # не https
        "file:///etc/passwd",
        "ftp://iris.who.int/x",
        "https://169.254.169.254/latest/meta-data/",  # link-local/metadata
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://iris.who.int.evil.example/x",  # суффиксная подделка хоста
        "https://evil.example/x",
        "https://iris.who.int:8443/x",  # нестандартный порт
        "https://iris.who.int:abc/x",  # порт, который urlsplit не парсит
    ],
)
def test_request_rejects_untrusted_url_before_network(url, monkeypatch):
    opened: list[int] = []
    monkeypatch.setattr(who, "_build_opener", lambda: opened.append(1))
    with pytest.raises(who.SourceError):
        who._request(WHOConfig(), url, accept="application/json")
    assert opened == []


def test_request_rejects_credentials_without_leaking_them(monkeypatch):
    monkeypatch.setattr(who, "_build_opener", lambda: pytest.fail("сеть не должна вызываться"))
    with pytest.raises(who.SourceError) as exc:
        who._request(WHOConfig(), "https://user:s3cret@iris.who.int/x", accept="application/json")
    assert "s3cret" not in str(exc.value)


def test_request_follows_same_host_redirect_chain(monkeypatch):
    opener = _RedirectChainOpener(
        ["/server/api/core/items/abc", "https://iris.who.int/server/api/bitstreams/1"]
    )
    monkeypatch.setattr(who, "_build_opener", lambda: opener)
    data = who._request(
        WHOConfig(), "https://iris.who.int/server/api/core/items/abc", accept="application/json"
    )
    assert data == b"payload"
    assert opener.urls == ["https://iris.who.int/server/api/core/items/abc"]


@pytest.mark.parametrize(
    "hop",
    [
        "https://evil.example/x",
        "//evil.example/x",
        "http://iris.who.int/server/api/items",
        "https://127.0.0.1/x",
        "https://iris.who.int@evil.example/x",
    ],
)
def test_redirect_hop_is_validated_like_the_first_request(hop, monkeypatch):
    opener = _RedirectChainOpener([hop])
    monkeypatch.setattr(who, "_build_opener", lambda: opener)
    with pytest.raises(who.SourceError):
        who._request(WHOConfig(), "https://iris.who.int/start", accept="application/json")


# --- Лицензия: положение стоит в конце документа, а не в первых 20k символов ---


def test_detect_license_scans_whole_document():
    body = "Recommendation text without any licence mention. " * 1100
    assert len(body) > 50_000
    cc = body + (
        "\nThis work is available under the Creative Commons "
        "Attribution-NonCommercial-ShareAlike 3.0 IGO licence (CC BY-NC-SA 3.0 IGO).\n"
    )
    assert who.detect_license(cc) == "CC BY-NC-SA 3.0 IGO"
    proprietary = body + "\n© World Health Organization 2023. All rights reserved.\n"
    assert who.detect_license(proprietary) == "proprietary"


def test_detect_license_without_statement_stays_empty():
    assert who.detect_license("No licence statement here. " * 2500) == ""


def test_detect_license_scan_is_bounded():
    text = "x" * who.LICENSE_SCAN_CHARS + " All rights reserved."
    assert who.detect_license(text) == ""


# --- Язык документа: берётся из выбранного bitstream, а не жёстко "en" ---


def test_stream_language_from_filename_then_metadata():
    assert who.stream_language({"name": "9789240049338-rus.pdf.txt"}, WHO_ITEM) == "ru"
    assert who.stream_language({"name": "9789240049338-eng.pdf.txt"}, WHO_ITEM) == "en"
    assert who.stream_language({"name": "doc.pdf.txt"}, WHO_ITEM) == "en"
    assert (
        who.stream_language(
            {"name": "doc.pdf.txt"}, {"metadata": {"dc.language.iso": [{"value": "eng"}]}}
        )
        == "en"
    )


def test_parse_item_uses_requested_language():
    meta = next(h for h in who.WHO_HANDLES if h.handle == "10665/374250")
    doc = who.parse_iris_item(WHO_ITEM, WHO_TEXT, meta, language="ru")
    assert doc.language == "ru"
    assert who.parse_iris_item(WHO_ITEM, WHO_TEXT, meta).language == "en"


def test_fetch_documents_labels_non_english_stream(monkeypatch):
    meta = next(h for h in who.WHO_HANDLES if h.handle == "10665/374250")
    item = {
        **WHO_ITEM,
        "_links": {"bundles": {"href": "https://iris.who.int/server/api/core/items/uuid/bundles"}},
    }

    def fake_get_json(cfg, url):
        if url.endswith("/bundles"):
            return {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "TEXT",
                            "_links": {"bitstreams": {"href": "https://iris.who.int/bitstreams"}},
                        }
                    ]
                }
            }
        if url.endswith("/bitstreams"):
            return {
                "_embedded": {
                    "bitstreams": [
                        {
                            "name": "9789240049338-rus.pdf.txt",
                            "_links": {"content": {"href": "https://iris.who.int/content/1"}},
                        }
                    ]
                }
            }
        return item

    text = "Психическое здоровье и депрессия. " * 200
    monkeypatch.setattr(who, "_get_json", fake_get_json)
    monkeypatch.setattr(who, "_request", lambda cfg, url, *, accept: text.encode("utf-8"))

    docs, _, errors = who.fetch_documents(WHOConfig(handles=(meta,)))
    assert errors == []
    assert docs[0].language == "ru"
