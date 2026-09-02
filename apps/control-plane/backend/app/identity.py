import json
from dataclasses import dataclass
from urllib import error, request

from . import config, db


class IdentityAuthError(Exception):
    pass


class IdentityUnavailableError(Exception):
    pass


class IdentityForbiddenError(Exception):
    pass


@dataclass(frozen=True)
class ExternalIdentity:
    uid: str
    name: str | None = None
    email: str | None = None
    avatar: str | None = None


def extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise IdentityAuthError("缺少登录 token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise IdentityAuthError("缺少登录 token")
    return token


def resolve_current_user_id(authorization: str | None) -> str:
    app_config = config.get_config()
    token = extract_bearer_token(authorization)

    if app_config.auth_mode == "local":
        user_id = db.get_session_user(token)
        if user_id is None:
            raise IdentityAuthError("登录已失效")
        return user_id

    identity = fetch_oa_current_user(token)
    # 平台 users 是 OA 用户在 Agent 平台内的授权主体映射，不负责认证。
    db.ensure_user_exists(identity.uid, "active")
    if not db.user_is_active(identity.uid):
        raise IdentityForbiddenError("当前用户不可用")
    return identity.uid


def fetch_oa_current_user(token: str) -> ExternalIdentity:
    app_config = config.get_config()
    if not app_config.oa_auth_me_url:
        raise IdentityUnavailableError("OA 鉴权地址未配置")

    http_request = request.Request(
        app_config.oa_auth_me_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with request.urlopen(
            http_request, timeout=app_config.oa_auth_timeout_seconds
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise IdentityAuthError("OA 登录已失效") from exc
        raise IdentityUnavailableError(f"OA 鉴权失败: HTTP {exc.code}") from exc
    except (TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise IdentityUnavailableError("OA 鉴权服务不可用") from exc

    uid = payload.get("uid") or payload.get("user_id") or payload.get("id")
    if not isinstance(uid, str) or not uid:
        raise IdentityUnavailableError("OA 鉴权响应缺少 uid")
    return ExternalIdentity(
        uid=uid,
        name=_optional_string(payload.get("name")),
        email=_optional_string(payload.get("email")),
        avatar=_optional_string(payload.get("avatar")),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
