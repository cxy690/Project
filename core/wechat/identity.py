"""用户自助绑定本人的服务号身份；二维码不含平台登录令牌。"""
from __future__ import annotations

import base64
import hashlib
import io
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from core.wechat import official
from db.base import get_session
from db.models import User, WeChatBindSession, WeChatIdentity, WeChatTarget, account_role


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _account(db: Any, guardian_id: str) -> User:
    user = db.get(User, guardian_id)
    if user is None or account_role(user.role) not in ("user", "admin") or not user.is_active or not settings.auth_enable:
        raise ValueError("需要有效的登录账号")
    return user


def _session(db: Any, guardian_id: str, session_id: str) -> WeChatBindSession:
    _account(db, guardian_id)
    row = db.get(WeChatBindSession, session_id)
    if row is None or row.guardian_id != guardian_id:
        raise ValueError("绑定会话不存在")
    if row.expires_at <= time.time() or row.app_id != settings.wechat_official_app_id.strip():
        raise ValueError("绑定二维码已过期，请重新发起")
    return row


def status(guardian_id: str) -> dict[str, Any]:
    with get_session() as db:
        _account(db, guardian_id)
        row = db.get(WeChatIdentity, guardian_id)
        valid = bool(row and row.app_id == settings.wechat_official_app_id.strip())
        return {"ok": True, "official": official.configuration(), "bound": bool(row),
                "enabled": bool(row and row.enabled), "identity_valid": valid,
                "bound_at": row.created_at if row else None, "user_id": guardian_id,
                "report_time": settings.wechat_report_time, "report_timezone": settings.wechat_report_timezone,
                "scheduler_enabled": bool(settings.wechat_scheduler_enable),
                "account_changed": bool(row and not valid)}


def create_session(guardian_id: str) -> dict[str, Any]:
    config = official.configuration()
    if not config["bind_ready"]:
        raise ValueError(config["bind_error"])
    ticket = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    sid = uuid.uuid4().hex
    now = time.time()
    expires_at = now + max(60, min(settings.wechat_official_bind_ttl_seconds, 900))
    start_url = official.bind_base_url() + "/api/wechat/official/authorize?" + urlencode({"ticket": ticket})
    qr_data_url = ""
    if not config.get("demo"):
        import qrcode
        image = qrcode.make(start_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        qr_data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    with get_session() as db:
        _account(db, guardian_id)
        db.execute(delete(WeChatBindSession).where(
            (WeChatBindSession.guardian_id == guardian_id) | (WeChatBindSession.expires_at < now)
        ))
        db.add(WeChatBindSession(id=sid, guardian_id=guardian_id,
                                app_id=settings.wechat_official_app_id.strip(),
                                ticket_hash=_hash(ticket), state_hash=_hash(state), status="waiting",
                                expires_at=expires_at))
        db.commit()
    return {"ok": True, "session_id": sid, "authorize_url": start_url, "expires_at": expires_at,
            "qr_data_url": qr_data_url, "demo": bool(config.get("demo"))}


def start_authorization(ticket: str) -> tuple[str, str]:
    if not official.configuration()["bind_ready"]:
        raise ValueError("服务号暂未就绪，请联系管理员")
    state = secrets.token_urlsafe(32)
    browser = secrets.token_urlsafe(32)
    with get_session() as db:
        row = db.scalar(select(WeChatBindSession).where(WeChatBindSession.ticket_hash == _hash(ticket)))
        if row is None:
            raise ValueError("绑定二维码无效")
        _account(db, row.guardian_id)
        if row.expires_at <= time.time() or row.app_id != settings.wechat_official_app_id.strip():
            raise ValueError("绑定二维码已过期，请重新发起")
        result = db.execute(update(WeChatBindSession).where(
            WeChatBindSession.id == row.id, WeChatBindSession.status == "waiting",
            WeChatBindSession.expires_at > time.time(),
        ).values(status="authorizing", state_hash=_hash(state), browser_hash=_hash(browser)))
        if result.rowcount != 1:
            raise ValueError("二维码已被使用，请在原页面刷新二维码")
        db.commit()
    return official.authorization_url(state), browser


def complete_authorization(state: str, code: str, browser: str) -> None:
    if not state or not code or not browser or not official.configuration()["bind_ready"]:
        raise ValueError("授权信息无效，请重新发起绑定")
    with get_session() as db:
        row = db.scalar(select(WeChatBindSession).where(WeChatBindSession.state_hash == _hash(state)))
        if row is None or row.app_id != settings.wechat_official_app_id.strip():
            raise ValueError("绑定会话无效")
        _account(db, row.guardian_id)
        if not secrets.compare_digest(row.browser_hash, _hash(browser)):
            raise ValueError("请在扫码授权的微信浏览器中完成操作")
        result = db.execute(update(WeChatBindSession).where(
            WeChatBindSession.id == row.id, WeChatBindSession.status == "authorizing",
            WeChatBindSession.expires_at > time.time(),
        ).values(status="exchanging"))
        if result.rowcount != 1:
            raise ValueError("绑定会话已失效或已使用")
        sid = row.id
        db.commit()
    try:
        openid = official.exchange_openid(code)
    except official.WeChatError:
        with get_session() as db:
            db.execute(update(WeChatBindSession).where(WeChatBindSession.id == sid).values(status="failed", openid=""))
            db.commit()
        raise
    with get_session() as db:
        result = db.execute(update(WeChatBindSession).where(
            WeChatBindSession.id == sid, WeChatBindSession.status == "exchanging",
            WeChatBindSession.expires_at > time.time(),
        ).values(status="authorized", openid=openid))
        db.commit()
        if result.rowcount != 1:
            raise ValueError("绑定会话已过期或已取消，请重新发起")


def session_status(guardian_id: str, session_id: str) -> dict[str, Any]:
    with get_session() as db:
        row = _session(db, guardian_id, session_id)
        return {"ok": True, "status": row.status, "expires_at": row.expires_at}


def confirm_binding(guardian_id: str, session_id: str) -> dict[str, Any]:
    with get_session() as db:
        row = _session(db, guardian_id, session_id)
        if row.status != "authorized" or not row.openid:
            raise ValueError("请先用本人微信完成扫码授权")
        openid, appid = row.openid, row.app_id
        claimed = db.execute(update(WeChatBindSession).where(
            WeChatBindSession.id == session_id, WeChatBindSession.status == "authorized",
            WeChatBindSession.expires_at > time.time(),
        ).values(status="consumed", openid=""))
        if claimed.rowcount != 1:
            raise ValueError("绑定会话已被使用")
        existing = db.scalar(select(WeChatIdentity).where(
            WeChatIdentity.app_id == appid, WeChatIdentity.openid == openid,
            WeChatIdentity.guardian_id != guardian_id,
        ))
        if existing is not None:
            raise ValueError("该微信已绑定其他用户账号，请先在原账号解绑")
        identity = db.get(WeChatIdentity, guardian_id)
        if identity is None:
            identity = WeChatIdentity(guardian_id=guardian_id)
            db.add(identity)
        identity.app_id, identity.openid, identity.enabled = appid, openid, True
        identity.updated_at = time.time()
        target = db.scalar(select(WeChatTarget).where(WeChatTarget.guardian_id == guardian_id))
        if target is None:
            target = WeChatTarget(guardian_id=guardian_id)
            db.add(target)
        target.channel, target.target, target.enabled = "official_account", "", True
        target.updated_at = time.time()
        try:
            db.execute(delete(WeChatBindSession).where(WeChatBindSession.guardian_id == guardian_id))
            db.commit()
        except IntegrityError as err:
            db.rollback()
            raise ValueError("该微信已绑定其他用户账号") from err
    return status(guardian_id)


def set_enabled(guardian_id: str, enabled: bool) -> dict[str, Any]:
    with get_session() as db:
        _account(db, guardian_id)
        row = db.get(WeChatIdentity, guardian_id)
        if row is None:
            raise ValueError("请先绑定微信")
        if row.app_id != settings.wechat_official_app_id.strip():
            raise ValueError("项目服务号已变更，请重新绑定")
        row.enabled = enabled
        row.updated_at = time.time()
        db.commit()
    return status(guardian_id)


def unbind(guardian_id: str) -> dict[str, Any]:
    with get_session() as db:
        _account(db, guardian_id)
        db.execute(delete(WeChatIdentity).where(WeChatIdentity.guardian_id == guardian_id))
        db.execute(delete(WeChatBindSession).where(WeChatBindSession.guardian_id == guardian_id))
        db.execute(delete(WeChatTarget).where(WeChatTarget.guardian_id == guardian_id,
                                            WeChatTarget.channel == "official_account"))
        db.commit()
    return status(guardian_id)


def cancel_session(guardian_id: str) -> dict[str, bool]:
    with get_session() as db:
        _account(db, guardian_id)
        db.execute(delete(WeChatBindSession).where(WeChatBindSession.guardian_id == guardian_id))
        db.commit()
    return {"ok": True}
