"""SQS AI consumer. Package this handler with app/ and requirements.txt."""
import json


def handler(event, context):
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
