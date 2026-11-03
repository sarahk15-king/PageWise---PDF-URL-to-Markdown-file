"""Tests for the product API. A stand-in engine is used, so no PDF library or AWS account is needed."""
import json
import sys
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeDoc:
    def __init__(self, quality):
        self.pages = 2
        self.warnings = []
        self.cost = {"ocr_pages": 1, "ai_cost_usd": 0.00289 if quality == "standard" else 0, "ai_input_tokens": 100,
                     "ai_output_tokens": 10}

    def to_json(self):
        return json.dumps({"stats": {"elements": 3, "average_confidence": 0.95, "ai_repaired": 0},
                           "elements": [{"type": "heading", "page": 1, "content": "Hi", "confidence": 0.93}]})

    def to_markdown(self):
        return "# Hi\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name in ("JOBS_TABLE", "API_KEYS_TABLE", "USAGE_TABLE", "JOBS_QUEUE_URL", "S3_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY_PEPPER", "test-pepper")
    fake = types.ModuleType("engine.pipeline")
    fake.extract_pdf = lambda path, image_store=None, quality="economy": FakeDoc(quality)
    fake.extract_url = lambda url, image_store=None: FakeDoc("economy")
    monkeypatch.setitem(sys.modules, "engine.pipeline", fake)
    from engine import jobs
    jobs._backend.clear()
    from api.products import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def get_key(client):
    return client.post("/v1/keys", data={"label": "t"}).json()["api_key"]


def wait_done(client, key, job_id):
    for _ in range(100):
        job = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": key}).json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


PDF = b"%PDF-1.4 fake"


def test_full_flow(client):
    key = get_key(client)
    h = {"X-API-Key": key}
    r = client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, data={"quality": "standard"}, headers=h)
    assert r.status_code == 202 and r.json()["status"] == "queued" and "key_id" not in r.json()
    job = wait_done(client, key, r.json()["job_id"])
    assert job["status"] == "done" and job["pages"] == 2
    assert job["cost"]["ai_usd"] == 0.00289 and job["cost"]["ocr_usd"] == 0.0015 and job["cost"]["total_usd"] == 0.00439
    assert client.get(f"/v1/jobs/{job['job_id']}/result", headers=h).text == "# Hi\n"
    j = client.get(f"/v1/jobs/{job['job_id']}/result?format=json", headers=h).json()
    assert j["elements"][0]["confidence"] == 0.93 and j["job"]["quality"] == "standard"
    assert client.get(f"/v1/jobs/{job['job_id']}/source", headers=h).content == PDF
    u = client.get("/v1/usage", headers=h).json()
    assert u["total"]["jobs"] == 1 and u["total"]["pages"] == 2 and u["total"]["cost_usd"] == 0.00439
    assert len(client.get("/v1/jobs", headers=h).json()["jobs"]) == 1


def test_auth_and_ownership(client):
    assert client.get("/v1/usage").status_code == 401
    assert client.get("/v1/usage", headers={"X-API-Key": "nope"}).status_code == 401
    a, b = get_key(client), get_key(client)
    r = client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, headers={"X-API-Key": a})
    jid = r.json()["job_id"]
    assert client.get(f"/v1/jobs/{jid}", headers={"X-API-Key": b}).status_code == 404  # not yours


def test_bad_input(client):
    h = {"X-API-Key": get_key(client)}
    assert client.post("/v1/jobs", data={"quality": "gold", "url": "https://example.com"}, headers=h).status_code == 400
    assert client.post("/v1/jobs", headers=h).status_code == 400  # neither
    assert client.post("/v1/jobs", files={"file": ("a.pdf", b"not a pdf")}, headers=h).status_code == 400
    assert client.post("/v1/jobs", data={"url": "http://127.0.0.1:8000/x"}, headers=h).status_code == 400
    assert client.post("/v1/jobs", data={"url": "file:///etc/passwd"}, headers=h).status_code == 400
    assert client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, data={"url": "https://example.com"}, headers=h).status_code == 400


def test_result_before_done_and_failed(client, monkeypatch):
    from engine import jobs
    key = get_key(client)
    h = {"X-API-Key": key}
    store, _ = jobs.backend()
    store.put_job({"job_id": "x1", "key_id": store.get_key(jobs.hash_key(key))["key_id"], "created_at": "now",
                   "status": "processing", "kind": "pdf", "source": "a", "quality": "economy"})
    r = client.get("/v1/jobs/x1/result", headers=h)
    assert r.status_code == 409 and r.json()["status"] == "processing"
    sys.modules["engine.pipeline"].extract_pdf = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken pdf"))
    r = client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, headers=h)
    job = wait_done(client, key, r.json()["job_id"])
    assert job["status"] == "failed" and "broken pdf" in job["error"]
    assert client.get(f"/v1/jobs/{job['job_id']}/result", headers=h).status_code == 422


def test_daily_limit_and_closed_signup(client, monkeypatch):
    monkeypatch.setenv("KEY_DAILY_JOBS", "1")
    h = {"X-API-Key": get_key(client)}
    assert client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, headers=h).status_code == 202
    assert client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, headers=h).status_code == 429
    monkeypatch.setenv("SIGNUP_OPEN", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    assert client.post("/v1/keys").status_code == 403
    assert client.post("/v1/keys", headers={"X-Admin-Token": "secret"}).status_code == 201


def test_key_not_stored(client, tmp_path):
    key = get_key(client)
    assert key.encode() not in b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())