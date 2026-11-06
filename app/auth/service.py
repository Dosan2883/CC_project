"""회원가입·로그인, 비밀번호 해싱과 JWT 발급·검증.

계정과 시도 제한은 auth/repository.py의 영속 저장소를 사용한다.
AWS에서는 AUTH_SECRET을 모든 채팅 서버가 공유하며, 로컬 키는 DB에 보존한다.
토큰은 stateless JWT이므로 로그아웃은 클라이언트에서 토큰을 버리는 방식이다.
"""
import hashlib
import hmac
import os
import re
import time
from typing import Protocol

import jwt

# 영문·숫자·_.-, 한글, 일본어(가나), 한자만 허용한다. 비슷하게 생긴 다른 문자로 남의 닉네임을 흉내 내는 것을 막는다.
NICK_RE = re.compile(r"[A-Za-z0-9_.\-가-힣ぁ-ゖァ-ヺー一-龥]{2,20}")
PBKDF2_ITERATIONS = 120_000
TOKEN_TTL = 7 * 24 * 3600


class AuthError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


class AccountRepo(Protocol):
    def get(self, key: str) -> dict | None: ...
    def create(self, key: str, record: dict) -> bool:
        """key가 이미 있으면 만들지 않고 False를 돌려준다."""
        ...


class TokenSigner:
    def __init__(self, secret: str, ttl: int = TOKEN_TTL):
        self.secret, self.ttl = secret, ttl

    def issue(self, nickname: str) -> str:
        now = int(time.time())
        return jwt.encode({"sub": nickname, "iat": now, "exp": now + self.ttl}, self.secret, algorithm="HS256")

    def verify(self, token: str) -> str | None:
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})["sub"]
        except jwt.PyJWTError:
            return None


def load_secret() -> str:
    secret = os.environ.get("AUTH_SECRET")
    if not secret:
        raise RuntimeError("AUTH_SECRET is required when loading a shared signing key")
    return secret


_DUMMY_SALT = os.urandom(16)


class AuthService:
    def __init__(self, repo: AccountRepo, signer: TokenSigner, throttle):
        self.repo, self.signer, self.throttle = repo, signer, throttle

    def signup(self, nickname: str, password: str) -> tuple[str, str]:
        nick = nickname.strip()
        if not NICK_RE.fullmatch(nick):
            raise AuthError(400, "INVALID_NICKNAME")
        if not 6 <= len(password) <= 64:
            raise AuthError(400, "INVALID_PASSWORD")
        salt = os.urandom(16)
        record = {"nickname": nick, "salt": salt, "iterations": PBKDF2_ITERATIONS,
                  "pwHash": hash_password(password, salt), "createdAt": int(time.time() * 1000)}
        if not self.repo.create(nick.casefold(), record):  # 대소문자만 다른 이름도 같은 닉네임으로 본다
            raise AuthError(409, "NICKNAME_TAKEN")
        return nick, self.signer.issue(nick)

    def login(self, nickname: str, password: str) -> tuple[str, str]:
        key = nickname.strip().casefold()
        if self.throttle.blocked(key):
            raise AuthError(429, "TOO_MANY_ATTEMPTS")
        acct = self.repo.get(key)
        # 없는 닉네임도 같은 계산을 거쳐서, 응답 시간으로 가입 여부를 알아내지 못하게 한다.
        digest = hash_password(password, acct["salt"] if acct else _DUMMY_SALT,
                               acct["iterations"] if acct else PBKDF2_ITERATIONS)
        if not acct or not hmac.compare_digest(digest, acct["pwHash"]):
            self.throttle.fail(key)
            raise AuthError(401, "INVALID_CREDENTIALS")  # 닉네임 오류와 비밀번호 오류를 구분하지 않는다
        self.throttle.clear(key)
        return acct["nickname"], self.signer.issue(acct["nickname"])

    def verify(self, token: str | None) -> str | None:
        """유효한 토큰이면 닉네임, 아니면 None. AWS에서는 Cognito JWT 검증으로 바뀌는 지점."""
        nick = self.signer.verify(token or "")
        if nick and self.repo.get(nick.casefold()):
            return nick
        return None
