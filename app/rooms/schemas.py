"""채팅방 생성 및 비공개방 입장 요청 모델."""
from typing import Literal

from pydantic import BaseModel


class CreateRoom(BaseModel):
    name: str
    visibility: Literal["public", "private"] = "public"
    password: str = ""


class EnterPrivate(BaseModel):
    name: str
    password: str
