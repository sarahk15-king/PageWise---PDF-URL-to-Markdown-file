"""The worker container: reads job messages from the SQS queue and runs them.

    python -m engine.worker

It is the command the Terraform worker task starts. Several workers can run at the same time; the queue hands
each message to one of them, and ECS autoscaling adds workers when the queue grows. A message that is not
deleted (the worker crashed) reappears after the visibility timeout and, after the allowed number of tries,
moves to the dead-letter queue. A job that fails cleanly is marked "failed" and its message is deleted.
"""
import json
import logging
import os
import signal
import sys

from . import jobs

log = logging.getLogger("pagewise.worker")
_running = True


def _stop(*_):
    global _running
    _running = False


def run():
    import boto3

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not jobs.use_aws():
        sys.exit("The worker needs JOBS_TABLE, API_KEYS_TABLE, USAGE_TABLE, JOBS_QUEUE_URL and S3_BUCKET.")
    signal.signal(signal.SIGTERM, _stop)  # ECS sends this on scale-in: finish the current job, then exit
    signal.signal(signal.SIGINT, _stop)
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    sqs = boto3.client("sqs", region_name=region)
    url = os.environ["JOBS_QUEUE_URL"]
    log.info("worker started, queue %s", url)
    while _running:
        response = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        for message in response.get("Messages", []):
            try:
                job_id = json.loads(message["Body"])["job_id"]
            except (ValueError, KeyError):
                log.warning("dropping a message that is not a job: %s", message.get("Body", "")[:100])
                sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
                continue
            log.info("job %s started", job_id)
            job = jobs.process_job(job_id)
            log.info("job %s %s in %ss, cost $%s", job_id, job and job.get("status"),
                     job and job.get("seconds"), job and (job.get("cost") or {}).get("total_usd"))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
    log.info("worker stopped")


if __name__ == "__main__":
    run()