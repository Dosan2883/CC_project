"""프로젝트 경로와 공통 설정. 실행 디렉터리에 의존하지 않는다."""
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env", override=False)
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
LANGUAGES = ["ko", "en", "ja", "zh-CN", "es", "fr"]
UI_LANGUAGES = ["ko", "en", "ja"]
BACKEND = os.getenv("DATA_BACKEND", "sqlite")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
MEDIA_QUEUE_URL = os.getenv("MEDIA_QUEUE_URL", "")
REDIS_URL = os.getenv("REDIS_URL", "")
# 서버 한 대(SINGLE_NODE=1)는 DynamoDB와 S3만 쓰고 SQS·Redis 없이 한 프로세스가 워커까지 돌린다. 여러 대는 아래 네 가지가 모두 필요하다.
SINGLE_NODE = os.getenv("SINGLE_NODE", "0") == "1"
RUN_MEDIA_WORKER = os.getenv("RUN_MEDIA_WORKER", "1" if BACKEND == "sqlite" or SINGLE_NODE else "0") == "1"
# 번역(AWS Lambda 함수 URL). 비워 두면 번역 API가 503을 돌려주고 나머지 기능은 그대로 동작한다.
TRANSLATE_URL = os.getenv("TRANSLATE_URL", "")
TRANSLATE_SECRET = os.getenv("TRANSLATE_SECRET", "")
TRANSLATE_HOURLY_LIMIT = int(os.getenv("TRANSLATE_HOURLY_LIMIT", "60"))


def aws_client(service):
    import boto3
    from botocore.config import Config
    options = {"signature_version": "s3v4"} if service == "s3" else {}
    return boto3.client(service, region_name=AWS_REGION, config=Config(**options, retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=30))


def create_database():
    from app.core.database import DynamoDatabase, SQLiteDatabase
    if BACKEND == "sqlite":
        return SQLiteDatabase(DATA_DIR / "messenger.sqlite3")
    if BACKEND != "dynamodb" or not DYNAMODB_TABLE:
        raise RuntimeError("DATA_BACKEND must be sqlite or dynamodb; DynamoDB requires DYNAMODB_TABLE")
    if SINGLE_NODE:
        if not os.getenv("AUTH_SECRET") or not S3_BUCKET:
            raise RuntimeError("Single-node DynamoDB mode requires AUTH_SECRET and S3_BUCKET")
    elif not os.getenv("AUTH_SECRET") or not S3_BUCKET or not MEDIA_QUEUE_URL or not REDIS_URL:
        raise RuntimeError("DynamoDB mode requires shared AUTH_SECRET, S3_BUCKET, MEDIA_QUEUE_URL and REDIS_URL")
    return DynamoDatabase(DYNAMODB_TABLE, aws_client("dynamodb"))
