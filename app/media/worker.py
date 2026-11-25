"""별도 프로세스/EC2 실행: python -m app.media.worker."""
import logging
import signal

from app.core import metrics
from app.core.runtime import close_notifications, media_svc


def main():
    logging.basicConfig(level=logging.INFO)
    stop_metrics = metrics.start("Worker")
    def stop(*_):
        media_svc.queue.stop_event.set()
        media_svc.queue.wake()

    signal.signal(signal.SIGTERM, stop)
    try:
        media_svc.queue.run()
    except KeyboardInterrupt:
        media_svc.queue.stop_event.set()
    finally:
        close_notifications()
        stop_metrics()


if __name__ == "__main__":
    main()
