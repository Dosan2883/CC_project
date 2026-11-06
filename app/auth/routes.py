"""회원가입, 로그인, 내 정보 API."""
from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import current_user
from app.auth.schemas import Credentials
from app.auth.service import AuthError
from app.core.runtime import auth

router = APIRouter()


def auth_http(e: AuthError) -> HTTPException:
    return HTTPException(e.status, e.code)


@router.post("/api/auth/signup")
def signup(body: Credentials):
    try:
        nickname, token = auth.signup(body.nickname, body.password)
    except AuthError as e:
        raise auth_http(e)
    return {"nickname": nickname, "token": token}


@router.post("/api/auth/login")
def login(body: Credentials):
    try:
        nickname, token = auth.login(body.nickname, body.password)
    except AuthError as e:
        raise auth_http(e)
    return {"nickname": nickname, "token": token}


@router.get("/api/auth/me")
def me(user: str = Depends(current_user)):
    return {"nickname": user}
