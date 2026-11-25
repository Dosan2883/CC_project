"""DB 상태를 복구 기준으로 사용하고 SQS는 작업 알림으로 사용한다."""
import json
import logging
import queue
import threading
import time
from app.core import metrics
from app.core.database import Change, Conflict

log = logging.getLogger(__name__)
ACTIVE_STATUSES = {"PENDING", "PROCESSING"}
RECOVERY_INTERVAL = 30


def pending_job_change(db, partition, job_id):
    pending = "PENDING_JOBS#" + partition
    record = db.get(pending, job_id)
    return Change(pending, job_id, record.version if record else None, {"jobId": job_id})


def pending_jobs(db, partition):
    """Backfill older active jobs once per database, then read only the pending index."""
    pending = "PENDING_JOBS#" + partition
    if not db.get("CONFIG", pending):
        for job in db.query(partition):
            if job.value["status"] in ACTIVE_STATUSES and not db.get(pending, job.sk):
                try:
                    db.commit([job.change(job.value, check=True), Change(pending, job.sk, None, {"jobId": job.sk})])
                except Conflict:
                    current = db.get(partition, job.sk)
                    if current and current.value["status"] in ACTIVE_STATUSES and not db.get(pending, job.sk):
                        raise
        try:
            db.commit([Change("CONFIG", pending, None, {"backfilled": True})])
        except Conflict:
            pass

    for marker in db.query(pending):
        job = db.get(partition, marker.sk)
        if job and job.value["status"] in ACTIVE_STATUSES:
            yield job
        else:
            guard = Change(partition, marker.sk, job.version if job else None, None, check=True)
            try:
                db.commit([guard, marker.change(None)])
            except Conflict:
                pass


class MediaWorker:
    def __init__(self, service, client=None, queue_url=""):
        self.service, self.client, self.url = service, client, queue_url
        self.stop_event = threading.Event()
        self.local_jobs = queue.Queue() if not client else None
        self.thread = None
        self.heartbeat_interval = 25
        self.last_metric = 0

    def send(self, job_id):
        if self.client:
            self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps({"jobId": job_id}))
        else:
            self.local_jobs.put(job_id)

    def wake(self):
        if self.local_jobs is not None:
            self.local_jobs.put(None)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, name="media-worker", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake()
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                log.warning("worker still finishing; lease permits another worker to recover after exit")

    def recover(self):
        if getattr(self.service, "jobs_partition", "") == "AI_JOBS":
            from app.ai.refresh import recover
            recover(self.service)
        partition = getattr(self.service, "jobs_partition", "MEDIA_JOBS")
        records = list(pending_jobs(self.service.db, partition))
        now = time.monotonic()
        if metrics.ENABLED and (self.last_metric == 0 or now - self.last_metric >= 60):
            metrics.record("PendingJobs", sum(row.value["status"] == "PENDING" for row in records),
                           Queue="AI" if partition == "AI_JOBS" else "Media")
            self.last_metric = now
        for record in records:
            job = record.value
            if job["status"] in ("PENDING", "PROCESSING") and job.get("leaseUntil", 0) < time.time() and job.get("availableAt", 0) <= time.time():
                if self.stop_event.is_set():
                    return
                if self.client:
                    self.send(job["jobId"])
                else:
                    self.service.work(job["jobId"])

    def run(self):
        next_recovery = time.monotonic()
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_recovery:
                    self.recover()
                    next_recovery = time.monotonic() + RECOVERY_INTERVAL
                if not self.client:
                    try:
                        job_id = self.local_jobs.get(timeout=max(0, next_recovery - time.monotonic()))
                    except queue.Empty:
                        continue
                    if self.stop_event.is_set():
                        break
                    if job_id is None:
                        if getattr(self.service, "jobs_partition", "") == "AI_JOBS":
                            from app.ai.refresh import recover
                            recover(self.service)
                    else:
                        self.service.work(job_id)
                    continue
                response = self.client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1, WaitTimeSeconds=20, VisibilityTimeout=90)
                for message in response.get("Messages", []):
                    self.process_message(message)
            except Exception:
                log.exception("Media worker iteration failed; pending jobs remain durable")
                self.stop_event.wait(1)

    def process_message(self, message):
        handle = message["ReceiptHandle"]
        heartbeat_stop = threading.Event()

        def heartbeat():
            while not heartbeat_stop.wait(self.heartbeat_interval):
                try:
                    self.client.change_message_visibility(QueueUrl=self.url, ReceiptHandle=handle, VisibilityTimeout=90)
                except Exception:
                    log.exception("SQS visibility extension failed")

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            try:
                job_id = json.loads(message["Body"])["jobId"]
                if not isinstance(job_id, str):
                    return  # 잘못된 메시지는 삭제하지 않아 SQS가 DLQ로 이동하게 한다.
            except (ValueError, KeyError, TypeError):
                return
            terminal = self.service.work(job_id)
            if terminal:
                self.client.delete_message(QueueUrl=self.url, ReceiptHandle=handle)
            else:
                self.client.change_message_visibility(QueueUrl=self.url, ReceiptHandle=handle, VisibilityTimeout=15)
        finally:
            heartbeat_stop.set()
            thread.join(timeout=2)
