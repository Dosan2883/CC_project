"""인증 요청 모델."""
from pydantic import BaseModel
from typing import Literal


class Credentials(BaseModel):
    nickname: str
    password: str


class Signup(Credentials):
    uiLocale: Literal["ko", "en", "ja"] = "ko"
