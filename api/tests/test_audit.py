# Контракт: docs/04 §5 (audit append-only); docs/06 §3 (audit_append_only, admin audit list)
import pyotp
import pytest
from sqlalchemy import text

from app import db
from app.models import AuditLog, User

from .conftest import auth_header, login, register


def test_audit_log_append_only(engine):
    with db.SessionLocal() as s:
        user = User(email="au@test.ru", password_hash="x", role="ADMIN")
        s.add(user)
        s.flush()
        s.add(AuditLog(actor_id=user.id, action="TEST", object_type="user", object_id=str(user.id)))
        s.commit()
        with pytest.raises(Exception):
            s.execute(text("UPDATE audit_log SET action='HACKED'"))
        s.rollback()
        assert s.query(AuditLog).count() == 1


def test_admin_audit_list(client):
    reg = register(client, "adm@test.ru", role="ADMIN")
    data = login(client, "adm@test.ru", otp=pyotp.TOTP(reg["totp_secret"]).now())
    h = auth_header(data["access_token"])

    with db.SessionLocal() as s:
        admin = s.query(User).filter_by(email="adm@test.ru").one()
        s.add(AuditLog(actor_id=admin.id, action="TEST_EVENT", object_type="user", object_id=str(admin.id)))
        s.commit()

    resp = client.get("/api/v1/admin/audit", headers=h)
    assert resp.status_code == 200
    assert "TEST_EVENT" in [e["action"] for e in resp.json()["items"]]