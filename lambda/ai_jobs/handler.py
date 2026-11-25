"""SQS AI consumer. Package this handler with app/ and requirements.txt."""
import json
import os


def load_secrets():
    names = {key: os.environ[f"{key}_PARAMETER"] for key in ("AUTH_SECRET", "TRANSLATE_SECRET") if not os.getenv(key)}
    if names:
        import boto3
        from botocore.config import Config
        ssm = boto3.client("ssm", config=Config(connect_timeout=5, read_timeout=10))
        values = {}
        for key, name in names.items():
            parameter = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
            if parameter["Type"] != "SecureString" or not parameter["Value"]:
                raise RuntimeError(f"{key} must use a non-empty SecureString")
            values[key] = parameter["Value"]
        os.environ.update(values)


def handler(event, context):
    load_secrets()
    from app.core import metrics
    try:
        return process(event)
    finally:
        metrics.flush()


def process(event):
    from app.core.runtime import ai_jobs
    if event.get("source") == "aws.events":
        ai_jobs.queue.recover()
        return {"recovered": True}
    failures = []
    for record in event.get("Records", []):
        try:
            job_id = json.loads(record["body"])["jobId"]
            if not isinstance(job_id, str) or not ai_jobs.work(job_id):
                failures.append({"itemIdentifier": record["messageId"]})
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
