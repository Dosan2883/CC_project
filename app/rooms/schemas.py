"""채팅방 생성 및 비공개방 입장 요청 모델."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateRoom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=100)
    visibility: Literal["public", "private"] = "public"
    password: str = Field(default="", max_length=128)


class EnterPrivate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=100)
    password: str = Field(max_length=128)
