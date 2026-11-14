"""python -m scripts.migrate_to_aws [--dry-run]

로컬 모드(SQLite + data 폴더)의 데이터를 DynamoDB와 S3로 복사한다. 원본은 지우지 않고, 이미 AWS에 있는 항목은 덮어쓰지 않는다.
  - SQLite의 records 표(계정, 방, 메시지, 요약 목록 등) → DynamoDB 테이블
  - data/original, data/extracted, data/ai 의 파일 → S3(같은 키)
CONFIG 파티션(예전 로컬 서명 키)은 비밀 값이라 복사하지 않는다. AWS 모드는 .env의 AUTH_SECRET을 쓴다.
설정(.env)은 DATA_BACKEND=dynamodb 상태에서 실행하고, EC2 인스턴스 역할로 쓴다.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SKIP_PARTITIONS = {"CONFIG"}
FILE_FOLDERS = ("original", "extracted", "ai")


def copy_records(sqlite_path, dynamodb, table, dry_run=False):
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    copied = skipped = 0
    for pk, sk, version, payload in connection.execute("SELECT pk, sk, version, payload FROM records ORDER BY pk, sk"):
        if pk in SKIP_PARTITIONS:
            skipped += 1
            continue
        if not dry_run:
            try:
                dynamodb.put_item(TableName=table, ConditionExpression="attribute_not_exists(pk)",
                                  Item={"pk": {"S": pk}, "sk": {"S": sk}, "version": {"N": str(version)}, "payload": {"S": payload}})
            except dynamodb.exceptions.ConditionalCheckFailedException:
                skipped += 1  # 이미 있는 항목은 덮어쓰지 않는다
                continue
        copied += 1
    connection.close()
    return copied, skipped


def copy_files(data_dir, s3, bucket, dry_run=False):
    copied = skipped = 0
    for folder in FILE_FOLDERS:
        root = Path(data_dir) / folder
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            key = path.relative_to(data_dir).as_posix()
            if exists(s3, bucket, key):
                skipped += 1
                continue
            if not dry_run:
                s3.upload_file(str(path), bucket, key)
            copied += 1
    return copied, skipped


def exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="실제로 쓰지 않고 몇 개를 옮길지만 센다")
    parser.add_argument("--sqlite", default=None)
    args = parser.parse_args(argv)
    from app.core import config
    if config.BACKEND != "dynamodb" or not config.DYNAMODB_TABLE or not config.S3_BUCKET:
        raise SystemExit("먼저 .env에 DATA_BACKEND=dynamodb, DYNAMODB_TABLE, S3_BUCKET을 설정하세요(scripts.setup_aws --write-env).")
    sqlite_path = Path(args.sqlite) if args.sqlite else config.DATA_DIR / "messenger.sqlite3"
    if not sqlite_path.exists():
        raise SystemExit(f"로컬 DB가 없습니다: {sqlite_path}")
    label = "(미리 보기) " if args.dry_run else ""
    records = copy_records(sqlite_path, config.aws_client("dynamodb"), config.DYNAMODB_TABLE, args.dry_run)
    print(f"{label}DynamoDB: {records[0]}개 복사, {records[1]}개 건너뜀")
    files = copy_files(config.DATA_DIR, config.aws_client("s3"), config.S3_BUCKET, args.dry_run)
    print(f"{label}S3: 파일 {files[0]}개 복사, {files[1]}개 건너뜀")


if __name__ == "__main__":
    main()
