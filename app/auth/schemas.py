"""인증 요청 모델."""
from pydantic import BaseModel


class Credentials(BaseModel):
    nickname: str
    password: str
