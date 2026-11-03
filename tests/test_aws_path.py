"""The AWS path (DynamoDB, S3, SQS and the worker) against moto, an in-memory copy of AWS. No account is used."""
import json
import sys
import types

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from test_products import FakeDoc, PDF


@pytest.fixture()
def aws(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        db = boto3.client("dynamodb")
        db.create_table(TableName="pw-jobs", BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"},
                                              {"AttributeName": "key_id", "AttributeType": "S"},
                                              {"AttributeName": "created_at", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
                        GlobalSecondaryIndexes=[{"IndexName": "by_key", "Projection": {"ProjectionType": "ALL"},
                                                 "KeySchema": [{"AttributeName": "key_id", "KeyType": "HASH"},
                                                               {"AttributeName": "created_at", "KeyType": "RANGE"}]}])
        db.create_table(TableName="pw-keys", BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "key_hash", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "key_hash", "KeyType": "HASH"}])
        db.create_table(TableName="pw-usage", BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "key_id", "AttributeType": "S"},
                                              {"AttributeName": "period", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "key_id", "KeyType": "HASH"},
                                   {"AttributeName": "period", "KeyType": "RANGE"}])
        boto3.client("s3").create_bucket(Bucket="pw-data")
        queue_url = boto3.client("sqs").create_queue(QueueName="pw-jobs")["QueueUrl"]
        for name, value in dict(JOBS_TABLE="pw-jobs", API_KEYS_TABLE="pw-keys", USAGE_TABLE="pw-usage",
                                S3_BUCKET="pw-data", JOBS_QUEUE_URL=queue_url, API_KEY_PEPPER="p").items():
            monkeypatch.setenv(name, value)
        fake = types.ModuleType("engine.pipeline")
        fake.extract_pdf = lambda path, image_store=None, quality="economy": FakeDoc(quality)
        fake.extract_url = lambda url, image_store=None: FakeDoc("economy")
        monkeypatch.setitem(sys.modules, "engine.pipeline", fake)
        from engine import jobs
        jobs._backend.clear()
        from api.products import router
        app = FastAPI()
        app.include_router(router)
        yield TestClient(app), queue_url, jobs


def test_submit_queue_worker_result(aws, monkeypatch):
    client, queue_url, jobs = aws
    assert jobs.use_aws()
    key = client.post("/v1/keys", data={"label": "t"}).json()["api_key"]
    h = {"X-API-Key": key}
    job_id = client.post("/v1/jobs", files={"file": ("a.pdf", PDF)}, data={"quality": "standard"}, headers=h).json()["job_id"]
    assert client.get(f"/v1/jobs/{job_id}", headers=h).json()["status"] == "queued"
    sqs = boto3.client("sqs")
    assert sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"])[
        "Attributes"]["ApproximateNumberOfMessages"] == "1"

    from engine import worker
    original = jobs.process_job

    def once(j):
        result = original(j)
        worker._running = False  # stop after the first job
        return result
    monkeypatch.setattr(worker.jobs, "process_job", once)
    worker._running = True
    worker.run()

    job = client.get(f"/v1/jobs/{job_id}", headers=h).json()
    assert job["status"] == "done" and job["cost"]["total_usd"] == 0.00439
    assert client.get(f"/v1/jobs/{job_id}/result?format=json", headers=h).json()["elements"][0]["content"] == "Hi"
    assert boto3.client("s3").get_object(Bucket="pw-data", Key=f"jobs/{job_id}/result.md")["ServerSideEncryption"] == "AES256"
    u = client.get("/v1/usage", headers=h).json()
    assert u["total"]["jobs"] == 1 and u["total"]["cost_usd"] == 0.00439
    assert sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"])[
        "Attributes"]["ApproximateNumberOfMessages"] == "0"  # the message was deleted
    assert len(client.get("/v1/jobs", headers=h).json()["jobs"]) == 1


def test_url_job_and_public_check(aws, monkeypatch):
    client, _, jobs = aws
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("93.184.216.34", 443))])
    h = {"X-API-Key": client.post("/v1/keys").json()["api_key"]}
    r = client.post("/v1/jobs", data={"url": "https://example.com/paper"}, headers=h)
    assert r.status_code == 202 and r.json()["kind"] == "url"
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.5", 443))])
    assert client.post("/v1/jobs", data={"url": "https://internal.example"}, headers=h).status_code == 400