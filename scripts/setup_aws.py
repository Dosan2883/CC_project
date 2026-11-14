"""python -m scripts.setup_aws --table NAME --bucket NAME [--write-env]

서버 한 대에서 DynamoDB와 S3만 쓰는 구성을 준비한다(SQS·Redis 없이). 이미 있는 리소스는 건드리지 않고 그대로 쓴다.
  1. DynamoDB 테이블(파티션 키 pk, 정렬 키 sk, 모두 문자열, 온디맨드 요금)
  2. S3 버킷(퍼블릭 액세스 전체 차단, 브라우저 직접 업로드용 CORS)
  3. --write-env: .env에 AWS 모드 설정을 쓴다. 서명 키(AUTH_SECRET)는 새로 만들어 .env에만 저장하고 화면에 출력하지 않는다.
EC2의 인스턴스 역할(LabInstanceProfile)로 실행한다. 비밀 값은 인자로 받지 않는다.
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CORS = {"CORSRules": [{
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    # 서버 IP가 바뀌어도 업로드되게 모든 출처를 허용한다. 버킷은 비공개이고 업로드·재생은 서명된 주소로만 된다.
    "AllowedOrigins": ["*"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000,
}]}


def ensure_table(client, name):
    try:
        client.describe_table(TableName=name)
        return "이미 있음"
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        BillingMode="PAY_PER_REQUEST")
    client.get_waiter("table_exists").wait(TableName=name)
    return "만듦"


def ensure_bucket(client, name, region):
    try:
        client.head_bucket(Bucket=name)
        created = "이미 있음"
    except client.exceptions.ClientError as error:
        code = error.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise  # 403이면 다른 계정이 쓰는 이름이다
        args = {"Bucket": name}
        if region != "us-east-1":
            args["CreateBucketConfiguration"] = {"LocationConstraint": region}
        client.create_bucket(**args)
        created = "만듦"
    client.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    client.put_bucket_cors(Bucket=name, CORSConfiguration=CORS)
    return created


def upsert_env(path, values):
    """path의 .env에서 values의 키만 바꾸거나 덧붙인다. 다른 줄(번역 주소·비밀 값 등)은 그대로 둔다."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def existing_keys(path):
    path = Path(path)
    if not path.exists():
        return set()
    return {line.split("=", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--write-env", action="store_true")
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)
    from app.core import config
    region = args.region or config.AWS_REGION
    config.AWS_REGION = region
    print("DynamoDB 테이블:", ensure_table(config.aws_client("dynamodb"), args.table))
    print("S3 버킷:", ensure_bucket(config.aws_client("s3"), args.bucket, region))
    if args.write_env:
        env_file = Path(args.env_file) if args.env_file else config.BASE_DIR / ".env"
        values = {"DATA_BACKEND": "dynamodb", "DYNAMODB_TABLE": args.table, "S3_BUCKET": args.bucket, "AWS_REGION": region,
                  "SINGLE_NODE": "1", "RUN_MEDIA_WORKER": "1"}
        if "AUTH_SECRET" not in existing_keys(env_file):  # 이미 있으면 바꾸지 않는다(바꾸면 로그인이 풀린다)
            values["AUTH_SECRET"] = secrets.token_urlsafe(48)
        upsert_env(env_file, values)
        print(f".env 갱신: {', '.join(k for k in values if k != 'AUTH_SECRET')}" + (" (서명 키 AUTH_SECRET 새로 저장)" if "AUTH_SECRET" in values else ""))
    print("다음: python -m scripts.check_aws 로 연결을 확인하세요.")


if __name__ == "__main__":
    main()
