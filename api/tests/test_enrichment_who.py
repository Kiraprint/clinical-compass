"""Коннектор ВОЗ (IRIS): лицензия, TEXT bitstream, белый список (оффлайн)."""

from __future__ import annotations

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
