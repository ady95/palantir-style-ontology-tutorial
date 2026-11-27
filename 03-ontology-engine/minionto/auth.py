"""인증 — 요청한 실행자가 누구인지 확인한다 (21장)

19장의 API 키 파일(토큰 → 실행자)은 키가 새면 끝까지 유효하고, 할 수 있는 일의 범위도 없었다.
여기서는 서명된 토큰(JWT)으로 바꾼다.

    auth = JwtAuth(secret)                                   # .env 의 MINIONTO_TOKEN_SECRET
    token = auth.issue(sub="agent-01", role="AI비서", actor_type="agent",
                       scopes=["objects:read", "actions:apply"], ttl=timedelta(hours=1))
    principal = auth.authenticate(token)                     # 서명·만료·발급자·대상 확인
    principal.actor   # {"id": "agent-01", "role": "AI비서", "type": "agent", "token_id": "…"}

토큰에 담는 것
- sub·role·actor_type: 실행자 (사람 human · AI 에이전트 agent · 배치 작업 system)
- scope: 이 토큰으로 할 수 있는 API 범위 (아래 SCOPES)
- exp·iat·jti: 만료 시각 · 발급 시각 · 토큰 ID (감사 기록에 남는다)
- iss·aud: 누가 발급했고 누구에게 쓰는 토큰인가

실습은 공유 비밀키(HS256)로 직접 발급한다. 운영에서는 사내 인증 서버(OIDC)가 개인키로 서명(RS256 등)하고,
API 는 공개키 목록(JWKS)으로 검증만 한다 — PyJWT 의 PyJWKClient 를 쓰면 같은 authenticate() 구조로 바꿀 수 있다.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import jwt

SCOPES = {
    "objects:read": "객체 조회·검색·집계, 지금 가능한 Action, 상태 이력",
    "functions:call": "Function 호출",
    "actions:apply": "Action 실행 (dry-run 포함)과 실행 가능 대상 조회",
    "approvals:decide": "승인 대기 목록과 승인·반려",
}
ACTOR_TYPES = ("human", "agent", "system")


class AuthError(Exception):
    """인증 실패 (API 에서는 401)"""


@dataclass
class Principal:
    """인증된 호출자"""
    actor: dict
    scopes: frozenset[str]
    method: str                         # jwt | api_key
    token_id: str | None = None
    expires_at: datetime | None = None
    claims: dict = field(default_factory=dict)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


class ApiKeyAuth:
    """19장의 개발용 키 파일 (모든 범위 허용, 만료 없음) — 실습 호환용"""

    def __init__(self, keys: dict[str, dict]):
        self.keys = keys

    def authenticate(self, token: str) -> Principal:
        actor = self.keys.get(token)
        if actor is None:
            raise AuthError("유효한 API 키가 아닙니다")
        return Principal(dict(actor), frozenset(SCOPES), "api_key")


class JwtAuth:
    def __init__(self, secret: str | None = None, *, issuer: str = "minionto-dev",
                 audience: str = "minionto-api", leeway: int = 0):
        secret = secret or os.getenv("MINIONTO_TOKEN_SECRET")
        if not secret or len(secret.encode()) < 32:
            raise AuthError("토큰 서명 키가 없거나 너무 짧습니다 (MINIONTO_TOKEN_SECRET, 32바이트 이상)")
        self.secret, self.issuer, self.audience, self.leeway = secret, issuer, audience, leeway

    def issue(self, *, sub: str, role: str, actor_type: str, scopes: list[str] | set[str],
              ttl: timedelta = timedelta(hours=1), now: datetime | None = None) -> str:
        if actor_type not in ACTOR_TYPES:
            raise AuthError(f"actor_type 은 {', '.join(ACTOR_TYPES)} 중 하나입니다")
        unknown = set(scopes) - set(SCOPES)
        if unknown:
            raise AuthError(f"알 수 없는 범위: {', '.join(sorted(unknown))}")
        now = now or datetime.now(timezone.utc)
        claims = {"iss": self.issuer, "aud": self.audience, "sub": sub, "role": role, "actor_type": actor_type,
                  "scope": " ".join(sorted(scopes)), "iat": now, "exp": now + ttl, "jti": str(uuid.uuid4())}
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def authenticate(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"],      # 'none' 등 다른 알고리즘 거부
                                audience=self.audience, issuer=self.issuer, leeway=self.leeway,
                                options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]})
        except jwt.ExpiredSignatureError:
            raise AuthError("토큰이 만료되었습니다. 다시 발급받으세요") from None
        except jwt.InvalidTokenError as e:
            raise AuthError(f"유효하지 않은 토큰입니다 ({type(e).__name__})") from None
        if claims.get("actor_type") not in ACTOR_TYPES or not claims.get("role"):
            raise AuthError("토큰에 실행자 정보(role·actor_type)가 없습니다")
        scopes = frozenset(claims.get("scope", "").split()) & frozenset(SCOPES)
        actor = {"id": claims["sub"], "role": claims["role"], "type": claims["actor_type"],
                 "token_id": claims["jti"]}
        return Principal(actor, scopes, "jwt", claims["jti"],
                         datetime.fromtimestamp(claims["exp"], timezone.utc), claims)
