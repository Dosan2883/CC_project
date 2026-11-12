"""채팅 메시지 번역 API."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.ai.client import TranslationUnavailable
from app.auth.dependencies import current_user
from app.core.runtime import translation
from app.core.runtime import ai_jobs
from typing import Literal
from pydantic import model_validator


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["message", "page", "segment"]
    messageSeq: int | None = Field(default=None, ge=1)
    mediaId: str | None = None
    page: int | None = Field(default=None, ge=1)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    startChar: int = Field(default=0, ge=0)
    endChar: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid(self):
        if self.kind == "message" and self.messageSeq is None:
            raise ValueError("messageSeq required")
        if self.kind != "message" and not self.mediaId:
            raise ValueError("mediaId required")
        if self.kind == "page" and self.page is None:
            raise ValueError("page required")
        if self.kind == "segment" and (self.start is None or self.end is None or self.end <= self.start):
            raise ValueError("invalid segment range")
        return self


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roomId: str
    kind: Literal["translate", "summarize", "meeting-summary"]
    sourceLanguage: str = "auto"
    target: str
    sources: list[Source] = Field(min_length=1, max_length=20)
    consent: bool

router = APIRouter()


@router.post("/api/ai/jobs", status_code=202)
def create_job(body: JobRequest, user: str = Depends(current_user)):
    return ai_jobs.submit(body.model_dump(exclude_none=True), user)


@router.get("/api/ai/jobs/{job_id}")
def get_job(job_id: str, user: str = Depends(current_user)):
    return ai_jobs.get(job_id, user)


@router.post("/api/ai/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, user: str = Depends(current_user)):
    job = ai_jobs.get(job_id, user)
    if job["status"] != "FAILED":
        raise HTTPException(409, "JOB_NOT_FAILED")
    return ai_jobs.submit({k: job[k] for k in ("roomId", "kind", "sourceLanguage", "target", "sources")} | {"consent": True}, user)


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
