"""The product API: get a key, submit a PDF or a link, check the job, download Markdown or JSON, see usage and cost.

Mounted by api/main.py. Every route except POST /v1/keys needs the header  X-API-Key: <your key>.

    POST /v1/keys              form: label            -> a new API key (shown once)
    POST /v1/jobs              form: file or url, quality (economy|standard)  -> job id, status "queued"
    GET  /v1/jobs/{id}         status, quality, time, pages, cost of this job
    GET  /v1/jobs              your latest jobs
    GET  /v1/jobs/{id}/result  ?format=markdown (default) or json; 409 while the job is not done
    GET  /v1/jobs/{id}/source  the PDF you sent (for a side-by-side view)
    GET  /v1/usage             jobs, pages and cost per day for your key

Settings: API_KEY_PEPPER (secret), SIGNUP_OPEN (default true; false needs the X-Admin-Token header = ADMIN_TOKEN),
KEY_DAILY_JOBS (default 100), MAX_UPLOAD_MB (default 25). See engine/jobs.py for the storage settings.
"""
import hmac
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from engine import jobs

router = APIRouter(prefix="/v1", tags=["product API"])

_signups = defaultdict(deque)
_signups_lock = threading.Lock()
SIGNUPS_PER_HOUR = 10


def current_key(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Send your API key in the X-API-Key header.")
    store, _ = jobs.backend()
    found = store.get_key(jobs.hash_key(x_api_key.strip()))
    if not found:
        raise HTTPException(status_code=401, detail="That API key is not valid.")
    return found


def public_job(job):
    hidden = {"key_id"}
    return {k: v for k, v in job.items() if k not in hidden}


def own_job(job_id, key):
    store, _ = jobs.backend()
    job = store.get_job(job_id)
    if not job or job["key_id"] != key["key_id"]:
        raise HTTPException(status_code=404, detail="No job with that id.")  # same answer for someone else's job
    return job


@router.post("/keys", status_code=201)
def create_key(request: Request, label: str = Form(""), x_admin_token: str = Header(None)):
    if os.getenv("SIGNUP_OPEN", "true").strip().lower() not in ("1", "true", "yes", "on"):
        admin = os.getenv("ADMIN_TOKEN", "")
        if not admin or not x_admin_token or not hmac.compare_digest(admin, x_admin_token):
            raise HTTPException(status_code=403, detail="Key sign-up is closed. Ask the owner for a key.")
    who = request.client.host if request.client else "unknown"
    with _signups_lock:  # a stranger can get a key, but not thousands
        recent = _signups[who]
        while recent and time.time() - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= SIGNUPS_PER_HOUR:
            raise HTTPException(status_code=429, detail="Too many new keys from this address. Try again later.")
        recent.append(time.time())
    api_key, key_id, key_hash = jobs.new_api_key()
    store, _ = jobs.backend()
    store.put_key(key_hash, key_id, label.strip()[:60])
    return {"api_key": api_key, "key_id": key_id,
            "note": "Keep this key. It is shown only once. Send it in the X-API-Key header."}


@router.post("/jobs", status_code=202)
def submit_job(file: UploadFile = File(None), url: str = Form(""), quality: str = Form("economy"),
               key=Depends(current_key)):
    cfg = jobs.settings()
    quality = quality.strip().lower() or "economy"
    if quality not in jobs.QUALITY_LEVELS:
        raise HTTPException(status_code=400, detail="quality must be one of: " + ", ".join(jobs.QUALITY_LEVELS))
    url = url.strip()
    if bool(file and file.filename) == bool(url):
        raise HTTPException(status_code=400, detail="Send either a PDF file or a url, not both and not neither.")

    store, _ = jobs.backend()
    today = jobs.period_now()
    used = store.get_usage(key["key_id"]).get(today, {})
    if used.get("jobs_submitted", 0) >= cfg["daily_jobs"]:
        raise HTTPException(status_code=429, detail=f"Daily limit of {cfg['daily_jobs']} jobs reached for this key.")

    if url:
        if not jobs.is_public_url(url):
            raise HTTPException(status_code=400, detail="The url must be a public http or https link.")
        job = jobs.create_job(key["key_id"], "url", url, quality)
    else:
        data = file.file.read(cfg["max_mb"] * 1024 * 1024 + 1)
        if len(data) > cfg["max_mb"] * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"The file is larger than {cfg['max_mb']} MB.")
        if not data.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="The file is not a PDF.")
        job = jobs.create_job(key["key_id"], "pdf", file.filename[:120], quality, pdf_bytes=data)
    store.add_usage(key["key_id"], today, jobs_submitted=1)
    return public_job(job)


@router.get("/jobs/{job_id}")
def job_status(job_id: str, key=Depends(current_key)):
    return public_job(own_job(job_id, key))


@router.get("/jobs")
def my_jobs(limit: int = 20, key=Depends(current_key)):
    store, _ = jobs.backend()
    return {"jobs": [public_job(j) for j in store.list_jobs(key["key_id"], max(1, min(limit, 100)))]}


@router.get("/jobs/{job_id}/result")
def job_result(job_id: str, format: str = "markdown", key=Depends(current_key)):
    job = own_job(job_id, key)
    if job["status"] == "failed":
        raise HTTPException(status_code=422, detail=job.get("error", "The job failed."))
    if job["status"] != "done":
        return JSONResponse(status_code=409, content={"detail": "The job is not finished yet.", "status": job["status"]})
    _, results = jobs.backend()
    if format == "json":
        data = results.get(job_id, "result.json")
        media = "application/json"
    elif format == "markdown":
        data = results.get(job_id, "result.md")
        media = "text/markdown; charset=utf-8"
    else:
        raise HTTPException(status_code=400, detail="format must be markdown or json.")
    if data is None:
        raise HTTPException(status_code=404, detail="The result is no longer available.")
    return Response(content=data, media_type=media)


@router.get("/jobs/{job_id}/source")
def job_source(job_id: str, key=Depends(current_key)):
    job = own_job(job_id, key)
    _, results = jobs.backend()
    data = results.get(job_id, "input.pdf") if job["kind"] == "pdf" else None
    if data is None:
        raise HTTPException(status_code=404, detail="There is no PDF for this job.")
    return Response(content=data, media_type="application/pdf")


@router.get("/usage")
def usage(key=Depends(current_key)):
    store, _ = jobs.backend()
    days = store.get_usage(key["key_id"])
    total = {"jobs": 0, "pages": 0, "cost_usd": 0.0, "seconds": 0.0, "failed_jobs": 0}
    for day in days.values():
        for name in total:
            total[name] += day.get(name, 0)
    total["cost_usd"] = round(total["cost_usd"], 6)
    total["seconds"] = round(total["seconds"], 2)
    return {"key_id": key["key_id"], "total": total, "by_day": days,
            "cost_note": "Cost is the AI repair cost plus OCR for scanned pages. Server time is not included."}