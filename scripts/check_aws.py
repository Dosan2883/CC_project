"""python -m scripts.check_aws: 기존 AWS 리소스를 읽어서 연결 조건만 확인한다."""
import json

from app.core import config


def main():
    if config.BACKEND != "dynamodb":
        raise SystemExit("Set DATA_BACKEND=dynamodb and fill in the AWS environment variables first.")
    database = config.create_database()
    table = database.client.describe_table(TableName=config.DYNAMODB_TABLE)["Table"]
    keys = {item["AttributeName"]: item["KeyType"] for item in table["KeySchema"]}
    if keys != {"pk": "HASH", "sk": "RANGE"}:
        raise SystemExit("DynamoDB table needs String partition key pk and String sort key sk.")
    attributes = {item["AttributeName"]: item["AttributeType"] for item in table["AttributeDefinitions"]}
    if attributes.get("pk") != "S" or attributes.get("sk") != "S":
        raise SystemExit("DynamoDB pk/sk must both be String attributes.")
    database.ping()
    s3 = config.aws_client("s3")
    s3.head_bucket(Bucket=config.S3_BUCKET)
    public = s3.get_public_access_block(Bucket=config.S3_BUCKET)["PublicAccessBlockConfiguration"]
    if not all(public.get(key) for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
        raise SystemExit("S3 bucket must block all public access.")
    if config.MEDIA_QUEUE_URL:
        attributes = config.aws_client("sqs").get_queue_attributes(QueueUrl=config.MEDIA_QUEUE_URL, AttributeNames=["RedrivePolicy", "VisibilityTimeout"])["Attributes"]
        redrive = json.loads(attributes.get("RedrivePolicy", "{}"))
        if not redrive.get("deadLetterTargetArn"):
            raise SystemExit("SQS media queue needs a dead-letter queue redrive policy.")
        if int(attributes.get("VisibilityTimeout", 0)) < 90:
            raise SystemExit("SQS media queue visibility timeout must be at least 90 seconds.")
    elif not config.SINGLE_NODE:
        raise SystemExit("MEDIA_QUEUE_URL is required unless SINGLE_NODE=1.")
    if config.REDIS_URL:
        from redis import Redis
        client = Redis.from_url(config.REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
        try:
            client.ping()
        finally:
            client.close()
    elif not config.SINGLE_NODE:
        raise SystemExit("REDIS_URL is required unless SINGLE_NODE=1.")
    checked = ["DynamoDB schema/read access", "private S3 bucket"]
    checked += ["SQS redrive policy"] if config.MEDIA_QUEUE_URL else []
    checked += ["Redis connectivity"] if config.REDIS_URL else []
    print(", ".join(checked) + ": OK" + ("" if not config.SINGLE_NODE else " (single node: SQS/Redis not used)"))
    print("Write permissions, browser CORS, network failover and processing still need an end-to-end AWS test.")


if __name__ == "__main__":
    main()
