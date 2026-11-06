"""DB 상태를 복구 기준으로 사용하고 SQS는 작업 알림으로 사용한다."""
import json
import logging
import threading
import time

log = logging.getLogger(__name__)


class MediaWorker:
    def __init__(self, service, client=None, queue_url=""):
        self.service, self.client, self.url = service, client, queue_url
        self.stop_event = threading.Event()
        self.thread = None
        self.heartbeat_interval = 25

    def send(self, job_id):
        if self.client:
            self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps({"jobId": job_id}))

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, name="media-worker", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                log.warning("worker still finishing; lease permits another worker to recover after exit")

    def recover(self):
        for record in self.service.db.query("MEDIA_JOBS"):
            job = record.value
            if job["status"] in ("PENDING", "PROCESSING") and job.get("leaseUntil", 0) < time.time() and job.get("availableAt", 0) <= time.time():
                if self.client:
                    self.send(job["jobId"])
                else:
                    self.service.work(job["jobId"])

    def run(self):
        last_recovery = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() - last_recovery >= (30 if self.client else 0.25):
                    self.recover()
                    last_recovery = time.monotonic()
                if not self.client:
                    self.stop_event.wait(0.2)
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
