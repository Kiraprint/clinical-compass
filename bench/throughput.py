"""Замер пропускной способности /assistant/ask на живом стеке.

Сам регистрирует свежего CLINICIAN (2FA-секрет возвращается в ответе register),
логинится с TOTP и меряет латентность/пропускную способность.
"""
import base64
import hashlib
import hmac
import json
import struct
import time
import urllib.request

BASE = "http://127.0.0.1:8090/api/v1"


def post(path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def totp(secret_b32, t=None):
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    ctr = int((t or time.time()) // 30)
    h = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


email = f"bench{int(time.time())}@test.ru"
reg = post("/auth/register", {"email": email, "password": "Bench-Pass-1", "role": "CLINICIAN"})
login = post("/auth/login", {"email": email, "password": "Bench-Pass-1",
                             "otp": totp(reg["totp_secret"])})
tok = login["access_token"]
print(f"login ok: role={login['role']}")

q = {"query": "Какой порог тяжести у шкалы PHQ-9?", "limit": 5}
for _ in range(5):
    post("/assistant/ask", q, tok)

lat = []
N = 60
t0 = time.time()
for _ in range(N):
    a = time.time()
    post("/assistant/ask", q, tok)
    lat.append(time.time() - a)
wall = time.time() - t0
lat.sort()
print(f"N={N} wall={wall:.2f}s RPS={N / wall:.2f}")
print(f"p50={lat[N // 2] * 1000:.0f}ms p95={lat[int(N * 0.95)] * 1000:.0f}ms max={lat[-1] * 1000:.0f}ms")
