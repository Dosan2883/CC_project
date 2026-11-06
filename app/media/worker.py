"""별도 프로세스/EC2 실행: python -m app.media.worker."""
import logging

from app.core.runtime import media_svc


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        media_svc.queue.run()
    except KeyboardInterrupt:
        media_svc.queue.stop_event.set()


if __name__ == "__main__":
    main()
