"""인증 요청 모델."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: str = Field(max_length=64)
    password: str = Field(max_length=128)


class Signup(Credentials):
    uiLocale: Literal["ko", "en", "ja"] = "ko"
