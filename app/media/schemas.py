"""파일 업로드 요청 모델."""
from pydantic import BaseModel


class UploadRequest(BaseModel):
    roomId: str
    filename: str
    size: int
