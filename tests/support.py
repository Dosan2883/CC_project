"""테스트는 실제 사용자 데이터와 AWS 리소스에 접근하지 않는다."""
import os
import tempfile

test_data = tempfile.TemporaryDirectory(prefix="messenger-tests-")
os.environ.update(DATA_BACKEND="sqlite", DATA_DIR=test_data.name, S3_BUCKET="", MEDIA_QUEUE_URL="", REDIS_URL="", RUN_MEDIA_WORKER="1", AUTH_SECRET="integration-test-only-signing-secret-0001")
