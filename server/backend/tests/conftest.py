import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
os.environ.update({"DATABASE_URL": f"sqlite:///{_tmp / 'test.db'}", "DATA_DIR": str(_tmp / "data"), "LLM_PROVIDER": "mock",
                   "SECRET_KEY": "test-secret-test-secret-test-secret-1234", "ENV": "dev", "ANALYZE_INTERVAL": "1",
                   "ANALYZE_MIN_GAP": "0", "DEMO_DELAY": "0.05", "ASR_PARTIALS": "false"})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.asr import engine as asr  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    asr.set_transcriber(lambda audio: "hello from the fake transcriber")
    with TestClient(app) as c:
        yield c


def register(client, email, password="password123"):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def bearer(client, email):
    r = register(client, email)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def admin(client):
    return bearer(client, "admin@example.com")


@pytest.fixture(scope="session")
def other(client, admin):
    return bearer(client, "user2@example.com")


def wait_for(fn, timeout=20):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.15)
    raise AssertionError("timeout")


def state(client, headers, sid, **params):
    q = {"sid": sid, "rev": -1, **params}
    return client.get("/api/state", headers=headers, params=q).json()
