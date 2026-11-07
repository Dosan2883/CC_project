"""채팅 메시지 번역 API."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.ai.client import TranslationUnavailable
from app.auth.dependencies import current_user
from app.core.runtime import translation

router = APIRouter()


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roomId: str
    messageSeq: int = Field(ge=1)
    target: str


class TranslatePageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mediaId: str
    page: int = Field(ge=1)
    target: str


@router.post("/api/ai/translate-page")
async def translate_page(body: TranslatePageRequest, user: str = Depends(current_user)):
    if not translation.translator.configured:
        raise HTTPException(503, "TRANSLATION_NOT_CONFIGURED")
    try:
        return await asyncio.to_thread(translation.translate_page, body.mediaId, body.page, body.target, user)
    except TranslationUnavailable:
        raise HTTPException(502, "TRANSLATION_UNAVAILABLE")


@router.post("/api/ai/translate")
async def translate(body: TranslateRequest, user: str = Depends(current_user)):
    if not translation.translator.configured:
        raise HTTPException(503, "TRANSLATION_NOT_CONFIGURED")
    try:
        return await asyncio.to_thread(translation.translate_message, body.roomId, body.messageSeq, body.target, user)
    except TranslationUnavailable:
        raise HTTPException(502, "TRANSLATION_UNAVAILABLE")
