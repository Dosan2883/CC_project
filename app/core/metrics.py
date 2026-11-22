"""Optional CloudWatch metrics. Payloads contain numbers and fixed labels only."""
from collections import defaultdict, deque
import logging
import os
from pathlib import Path
import threading
import time

from app.core.config import AWS_REGION, LANGUAGES

log = logging.getLogger(__name__)
ENABLED = os.getenv("CLOUDWATCH_METRICS", "0") == "1"
NAMESPACE = os.getenv("CLOUDWATCH_NAMESPACE", "Messenger")
pending = deque(maxlen=5000)
lock = threading.Lock()
client = None


def record(name, value=1, unit="Count", **labels):
    if ENABLED:
        with lock:
            pending.append((name, unit, tuple(sorted(labels.items())), value))


def extraction_duration(kind, language, start):
    code = language if language in LANGUAGES or language == "auto" else "und"
    record("ExtractionMs", (time.monotonic() - start) * 1000, "Milliseconds", Kind=kind, Language=code)


def flush():
    global client
    with lock:
        batch = list(pending)
        pending.clear()
    if not batch:
        return
    grouped = defaultdict(list)
    for name, unit, labels, value in batch:
        grouped[name, unit, labels].append(value)
    data = []
    for (name, unit, labels), values in grouped.items():
        for start in range(0, len(values), 150):
            data.append({"MetricName": name, "Unit": unit, "Values": values[start:start + 150],
                         "Dimensions": [{"Name": key, "Value": val} for key, val in labels]})
    try:
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("cloudwatch", region_name=AWS_REGION, config=Config(
                connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}))
        for start in range(0, len(data), 100):
            client.put_metric_data(Namespace=NAMESPACE, MetricData=data[start:start + 100])
    except Exception as error:
        log.warning("CloudWatch metrics delivery failed (%s)", type(error).__name__)


def host_memory():
    try:
        fields = dict((key, int(value.split()[0])) for key, value in
                      (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()))
        return 100 * (fields["MemTotal"] - fields["MemAvailable"]) / fields["MemTotal"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def start(role, connection_count=None):
    if not ENABLED:
        return lambda: None
    stop = threading.Event()

    def run():
        while not stop.wait(60):
            if connection_count is not None:
                record("WebSocketConnections", connection_count())
            used = host_memory()
            if used is not None:
                record("MemoryUsedPercent", used, "Percent", Role=role)
            flush()

    thread = threading.Thread(target=run, name="cloudwatch-metrics", daemon=True)
    thread.start()

    def close():
        stop.set()
        thread.join(timeout=5)
        if not thread.is_alive():
            flush()

    return close
