"""HTTP 요청의 로그인 사용자 확인."""
from fastapi import Header, HTTPException

from app.core.runtime import auth


def current_user(authorization: str | None = Header(default=None)) -> str:
    """요청자의 닉네임. 요청 본문이나 주소에 적힌 사용자 이름은 믿지 않고 토큰으로만 판단한다."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    user = auth.verify(token)
    if not user:
        raise HTTPException(401, "unauthorized")
    return user
