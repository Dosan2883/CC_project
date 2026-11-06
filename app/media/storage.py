"""파일 저장소.

데모는 로컬 폴더(LocalStorage)에 저장한다.
AWS에서는 같은 인터페이스(put/get)를 가진 S3Storage(boto3)로 교체한다.

키 규칙은 문서의 S3 구조를 그대로 따른다:
  original/{mediaId}          업로드 원본
  extracted/{mediaId}.json    추출된 텍스트(페이지별)
"""
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...


class LocalStorage:
    direct_upload = False
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # ../ 로 저장소 밖을 건드리는 키 차단
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put_original(self, key, data):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)

    def size(self, key):
        return self._path(key).stat().st_size

    def get_limited(self, key, limit):
        with self._path(key).open("rb") as stream:
            return stream.read(limit + 1)


class S3Storage:
    """브라우저 직접 업로드는 덮어쓰기를 막는 조건부 PUT으로 서명한다."""
    direct_upload = True

    def __init__(self, bucket, client):
        self.bucket, self.client = bucket, client

    def put(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as body:
            return body.read()

    def get_limited(self, key, limit):
        response = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{limit}")
        with response["Body"] as body:
            return body.read(limit + 1)

    def size(self, key):
        from botocore.exceptions import ClientError
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as error:
            if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(key) from error
            raise

    def upload_url(self, key, size, ttl):
        params = {"Bucket": self.bucket, "Key": key, "ContentLength": size, "ContentType": "application/octet-stream", "IfNoneMatch": "*"}
        url = self.client.generate_presigned_url("put_object", Params=params, ExpiresIn=ttl)
        return {"uploadUrl": url, "method": "PUT", "headers": {"Content-Type": "application/octet-stream", "If-None-Match": "*"}}
