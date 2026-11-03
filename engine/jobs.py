"""Jobs, API keys and usage for the Pagewise product API, plus the code that runs one job.

Two backends, chosen by settings, so the same code runs on a laptop, on the free live server and in AWS:

  local (default)  SQLite file and result files under DATA_DIR, jobs run on a background thread.
                   Needs no AWS keys, so it works on the live app.
  aws              DynamoDB (JOBS_TABLE, API_KEYS_TABLE, USAGE_TABLE), S3 (S3_BUCKET) and the SQS queue
                   (JOBS_QUEUE_URL). The worker container (python -m engine.worker) reads the queue.
                   Used when JOBS_TABLE, API_KEYS_TABLE, USAGE_TABLE, JOBS_QUEUE_URL and S3_BUCKET are all set.

Keys are never stored: only an HMAC-SHA256 of the key, made with API_KEY_PEPPER (a secret).
The cost of a job is the AI repair cost plus the OCR cost of scanned pages. Server time is not included.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

QUALITY_LEVELS = ("economy", "standard")
JOB_TTL_DAYS = 30


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def period_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def settings():
    return {
        "data_dir": Path(os.getenv("DATA_DIR", ".pagewise_data")),
        "pepper": os.getenv("API_KEY_PEPPER", "dev-pepper-change-me"),
        "ocr_price": float(os.getenv("OCR_PRICE_PER_PAGE", "0.0015")),
        "daily_jobs": int(os.getenv("KEY_DAILY_JOBS", "100")),
        "max_mb": int(os.getenv("MAX_UPLOAD_MB", "25")),
    }


def use_aws():
    names = ("JOBS_TABLE", "API_KEYS_TABLE", "USAGE_TABLE", "JOBS_QUEUE_URL", "S3_BUCKET")
    return all(os.getenv(n) for n in names)


# ---- API keys ----------------------------------------------------------------------------------

def hash_key(api_key):
    return hmac.new(settings()["pepper"].encode(), api_key.encode(), hashlib.sha256).hexdigest()


def new_api_key():
    """Returns (the key to show once, its key_id, the hash that is stored)."""
    secret = secrets.token_urlsafe(32)
    key_id = secrets.token_hex(4)
    api_key = f"pw_{key_id}_{secret}"
    return api_key, key_id, hash_key(api_key)


# ---- stores ------------------------------------------------------------------------------------

class SqliteStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("create table if not exists keys(key_hash text primary key, key_id text, label text, created_at text)")
            db.execute("create table if not exists jobs(job_id text primary key, key_id text, created_at text, data text)")
            db.execute("create index if not exists jobs_by_key on jobs(key_id, created_at)")
            db.execute("create table if not exists usage(key_id text, period text, data text, primary key(key_id, period))")

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    def put_key(self, key_hash, key_id, label):
        with self.lock, self._db() as db:
            db.execute("insert into keys values (?,?,?,?)", (key_hash, key_id, label, now_iso()))

    def get_key(self, key_hash):
        with self._db() as db:
            row = db.execute("select key_id, label, created_at from keys where key_hash=?", (key_hash,)).fetchone()
        return {"key_id": row[0], "label": row[1], "created_at": row[2]} if row else None

    def put_job(self, job):
        with self.lock, self._db() as db:
            db.execute("insert into jobs values (?,?,?,?)", (job["job_id"], job["key_id"], job["created_at"], json.dumps(job)))

    def get_job(self, job_id):
        with self._db() as db:
            row = db.execute("select data from jobs where job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_job(self, job_id, **fields):
        with self.lock, self._db() as db:
            row = db.execute("select data from jobs where job_id=?", (job_id,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(fields)
            db.execute("update jobs set data=? where job_id=?", (json.dumps(job), job_id))
        return job

    def list_jobs(self, key_id, limit=20):
        with self._db() as db:
            rows = db.execute("select data from jobs where key_id=? order by created_at desc limit ?", (key_id, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def add_usage(self, key_id, period, **amounts):
        with self.lock, self._db() as db:
            row = db.execute("select data from usage where key_id=? and period=?", (key_id, period)).fetchone()
            data = json.loads(row[0]) if row else {}
            for name, value in amounts.items():
                data[name] = round(data.get(name, 0) + value, 6)
            db.execute("insert or replace into usage values (?,?,?)", (key_id, period, json.dumps(data)))

    def get_usage(self, key_id):
        with self._db() as db:
            rows = db.execute("select period, data from usage where key_id=? order by period", (key_id,)).fetchall()
        return {period: json.loads(data) for period, data in rows}


def _plain(value):
    """DynamoDB hands numbers back as Decimal."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


class DynamoStore:
    def __init__(self):
        import boto3
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        db = boto3.resource("dynamodb", region_name=region)
        self.keys = db.Table(os.environ["API_KEYS_TABLE"])
        self.jobs = db.Table(os.environ["JOBS_TABLE"])
        self.usage = db.Table(os.environ["USAGE_TABLE"])

    def put_key(self, key_hash, key_id, label):
        self.keys.put_item(Item={"key_hash": key_hash, "key_id": key_id, "label": label, "created_at": now_iso()},
                           ConditionExpression="attribute_not_exists(key_hash)")

    def get_key(self, key_hash):
        item = self.keys.get_item(Key={"key_hash": key_hash}).get("Item")
        return {"key_id": item["key_id"], "label": item.get("label", ""), "created_at": item["created_at"]} if item else None

    def put_job(self, job):
        expires = int((datetime.now(timezone.utc) + timedelta(days=JOB_TTL_DAYS)).timestamp())
        self.jobs.put_item(Item={"job_id": job["job_id"], "key_id": job["key_id"], "created_at": job["created_at"],
                                 "expires_at": expires, "data": json.dumps(job)})

    def get_job(self, job_id):
        item = self.jobs.get_item(Key={"job_id": job_id}).get("Item")
        return json.loads(item["data"]) if item else None

    def update_job(self, job_id, **fields):
        job = self.get_job(job_id)
        if not job:
            return None
        job.update(fields)
        self.jobs.update_item(Key={"job_id": job_id}, UpdateExpression="SET #d = :d",
                              ExpressionAttributeNames={"#d": "data"}, ExpressionAttributeValues={":d": json.dumps(job)})
        return job

    def list_jobs(self, key_id, limit=20):
        from boto3.dynamodb.conditions import Key
        response = self.jobs.query(IndexName="by_key", KeyConditionExpression=Key("key_id").eq(key_id),
                                   ScanIndexForward=False, Limit=limit)
        return [json.loads(item["data"]) for item in response.get("Items", [])]

    def add_usage(self, key_id, period, **amounts):
        names = {f"#n{i}": name for i, name in enumerate(amounts)}
        values = {f":v{i}": Decimal(str(value)) for i, value in enumerate(amounts.values())}
        expression = "ADD " + ", ".join(f"#n{i} :v{i}" for i in range(len(amounts)))
        self.usage.update_item(Key={"key_id": key_id, "period": period}, UpdateExpression=expression,
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def get_usage(self, key_id):
        from boto3.dynamodb.conditions import Key
        items = self.usage.query(KeyConditionExpression=Key("key_id").eq(key_id)).get("Items", [])
        return {item["period"]: {k: _plain(v) for k, v in item.items() if k not in ("key_id", "period")} for item in items}


class LocalResults:
    """Inputs and results as files under DATA_DIR/results/<job_id>/."""

    def __init__(self, root):
        self.root = Path(root) / "results"

    def put(self, job_id, name, data, content_type="application/octet-stream"):
        path = self.root / job_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, job_id, name):
        path = self.root / job_id / name
        return path.read_bytes() if path.is_file() else None


class S3Results:
    """Inputs and results in the encrypted data bucket under jobs/<job_id>/."""

    def __init__(self):
        import boto3
        self.client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1")
        self.bucket = os.environ["S3_BUCKET"]

    def put(self, job_id, name, data, content_type="application/octet-stream"):
        self.client.put_object(Bucket=self.bucket, Key=f"jobs/{job_id}/{name}", Body=data,
                               ContentType=content_type, ServerSideEncryption="AES256")

    def get(self, job_id, name):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=f"jobs/{job_id}/{name}")["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None


_backend = {}


def backend():
    """(store, results) for this process. Built once."""
    if not _backend:
        if use_aws():
            _backend.update(store=DynamoStore(), results=S3Results())
        else:
            root = settings()["data_dir"]
            _backend.update(store=SqliteStore(root / "pagewise.db"), results=LocalResults(root))
    return _backend["store"], _backend["results"]


# ---- queue -------------------------------------------------------------------------------------

_local_queue = queue.Queue()
_local_thread = []


def enqueue(job_id):
    if use_aws():
        import boto3
        boto3.client("sqs", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1") \
            .send_message(QueueUrl=os.environ["JOBS_QUEUE_URL"], MessageBody=json.dumps({"job_id": job_id}))
        return
    _local_queue.put(job_id)
    if not _local_thread:
        thread = threading.Thread(target=_local_loop, daemon=True, name="pagewise-local-worker")
        thread.start()
        _local_thread.append(thread)


def _local_loop():
    while True:
        job_id = _local_queue.get()
        try:
            process_job(job_id)
        finally:
            _local_queue.task_done()


# ---- one job -----------------------------------------------------------------------------------

def is_public_url(url):
    """Only http(s) links to public addresses. The service fetches these, so private networks stay closed."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def create_job(key_id, kind, source, quality, pdf_bytes=None):
    store, results = backend()
    job_id = uuid.uuid4().hex
    job = {"job_id": job_id, "key_id": key_id, "created_at": now_iso(), "status": "queued",
           "kind": kind, "source": source, "quality": quality}
    if pdf_bytes is not None:
        results.put(job_id, "input.pdf", pdf_bytes, "application/pdf")
    store.put_job(job)
    enqueue(job_id)
    return job


def job_cost(doc):
    cfg = settings()
    ai = float(doc.cost.get("ai_cost_usd", 0) or 0)
    ocr_pages = int(doc.cost.get("ocr_pages", 0) or 0)
    ocr = ocr_pages * cfg["ocr_price"]
    return {"ai_usd": round(ai, 6), "ocr_pages": ocr_pages, "ocr_usd": round(ocr, 6), "total_usd": round(ai + ocr, 6),
            "ai_input_tokens": int(doc.cost.get("ai_input_tokens", 0) or 0),
            "ai_output_tokens": int(doc.cost.get("ai_output_tokens", 0) or 0)}


def process_job(job_id):
    """Runs one job end to end. Never raises: a failure becomes status "failed" with a short reason."""
    store, results = backend()
    job = store.get_job(job_id)
    if not job or job.get("status") not in ("queued", "processing"):
        return job
    started = time.time()
    store.update_job(job_id, status="processing", started_at=now_iso())
    tmp_path = None
    try:
        from engine.pipeline import extract_pdf, extract_url

        def save_image(name, png):
            results.put(job_id, f"images/{name}.png", png, "image/png")
            return f"images/{name}.png"

        if job["kind"] == "pdf":
            data = results.get(job_id, "input.pdf")
            if data is None:
                raise RuntimeError("the uploaded file is missing")
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            doc = extract_pdf(tmp_path, image_store=save_image, quality=job["quality"])
        else:
            doc = extract_url(job["source"], image_store=save_image)
        raw = doc.to_json()
        document = raw if isinstance(raw, dict) else json.loads(raw)
        markdown = doc.to_markdown()
        cost = job_cost(doc)
        seconds = round(time.time() - started, 2)
        pages = int(doc.pages or 1)
        document["job"] = {"job_id": job_id, "quality": job["quality"], "seconds": seconds, "cost": cost}
        results.put(job_id, "result.md", markdown.encode("utf-8"), "text/markdown")
        results.put(job_id, "result.json", json.dumps(document, ensure_ascii=False).encode("utf-8"), "application/json")
        stats = document.get("stats", {})
        store.update_job(job_id, status="done", finished_at=now_iso(), seconds=seconds, pages=pages, cost=cost,
                         elements=stats.get("elements"), average_confidence=stats.get("average_confidence"),
                         ai_repaired=stats.get("ai_repaired", 0), warnings=list(doc.warnings)[:20])
        store.add_usage(job["key_id"], period_now(), jobs=1, pages=pages, cost_usd=cost["total_usd"], seconds=seconds)
    except Exception as error:  # a bad file must not stop the worker
        store.update_job(job_id, status="failed", finished_at=now_iso(), error=str(error)[:300])
        store.add_usage(job["key_id"], period_now(), failed_jobs=1)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    return store.get_job(job_id)