"""指定用户的课程内容更新：保存不发送，调度到期后唯一领取。"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from config.settings import settings
from core.wechat import bindings, official, providers
from db.base import get_session
from db.models import User, WeChatUserNotice, account_role


def _user(db, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active or account_role(user.role) != "user":
        raise ValueError("请选择有效的用户账号")
    return user


def _view(row: WeChatUserNotice) -> dict[str, Any]:
    return {"id": row.id, "user_id": row.user_id, "title": row.title, "content": row.content,
            "send_at": row.send_at, "status": row.status, "channel": row.channel,
            "message_id": row.message_id, "error": row.error, "created_at": row.created_at,
            "sent_at": row.sent_at}


def list_notices(user_id: str = "") -> list[dict]:
    with get_session() as db:
        stmt = select(WeChatUserNotice)
        if user_id:
            stmt = stmt.where(WeChatUserNotice.user_id == user_id)
        rows = db.scalars(stmt.order_by(WeChatUserNotice.created_at.desc()).limit(200)).all()
        return [_view(row) for row in rows]


def preview(user_id: str, title: str, content: str) -> dict:
    with get_session() as db:
        user = _user(db, user_id)
        return {"ok": True, "user_id": user.id,
                "text": f"【课程更新】{title.strip()}\n{user.display_name or user.username}您好：\n{content.strip()}",
                "official": official.configuration()}


def save(user_id: str, title: str, content: str, send_at: datetime, notice_id: str = "") -> dict:
    if not settings.auth_enable or not settings.wechat_assistant_enable:
        raise ValueError("账号体系或微信助手已关闭")
    if not settings.wechat_course_scheduler_enable:
        raise ValueError("课程更新定时调度尚未开启，请管理员配置后再安排发送")
    title, content = title.strip(), content.strip()
    if not title or not content or len(title) > 80 or len(content) > 3000:
        raise ValueError("课程标题限 1–80 字，更新内容限 1–3000 字")
    if send_at.tzinfo is None or send_at.timestamp() <= time.time():
        raise ValueError("请选择包含时区的未来发送时间")
    with get_session() as db:
        _user(db, user_id)
        target = bindings.get_target(user_id)
        if not target or not target["enabled"]:
            raise ValueError("用户尚未绑定微信或已暂停通知")
        channel = target["channel"]
        if channel == "official_account":
            config = official.configuration()
            if not config["course_ready"]:
                raise ValueError(config["course_error"])
        row = db.get(WeChatUserNotice, notice_id) if notice_id else None
        if notice_id:
            if row is None or row.user_id != user_id:
                raise ValueError("课程更新不存在或不属于所选用户")
            changed = db.execute(update(WeChatUserNotice).where(
                WeChatUserNotice.id == notice_id, WeChatUserNotice.status == "pending",
            ).values(title=title, content=content, send_at=send_at.timestamp(), channel=channel))
            if changed.rowcount != 1:
                raise ValueError("仅待发送的课程更新可以修改")
        else:
            row = WeChatUserNotice(id=uuid.uuid4().hex, user_id=user_id, title=title, content=content,
                                   send_at=send_at.timestamp(), channel=channel, status="pending")
            db.add(row)
        db.commit()
        db.refresh(row)
        return _view(row)


def cancel(notice_id: str) -> None:
    with get_session() as db:
        changed = db.execute(update(WeChatUserNotice).where(
            WeChatUserNotice.id == notice_id, WeChatUserNotice.status == "pending",
        ).values(status="cancelled"))
        if changed.rowcount != 1:
            raise ValueError("仅待发送的课程更新可以取消")
        db.commit()


def dispatch(notice_id: str, now: float | None = None) -> dict:
    current = time.time() if now is None else now
    with get_session() as db:
        claimed = db.execute(update(WeChatUserNotice).where(
            WeChatUserNotice.id == notice_id, WeChatUserNotice.status == "pending",
            WeChatUserNotice.send_at <= current,
        ).values(status="sending"))
        if claimed.rowcount != 1:
            db.rollback()
            return {"id": notice_id, "skipped": True}
        db.commit()
    with get_session() as db:
        row = db.get(WeChatUserNotice, notice_id)
        try:
            user = _user(db, row.user_id)
            if current - row.send_at > 1800:
                raise ValueError("已超过 30 分钟补发窗口，请重新安排")
            target = bindings.get_target(row.user_id)
            if not target or not target["enabled"] or target["channel"] != row.channel:
                raise ValueError("用户已暂停、解绑或通知渠道已变更")
            payload = {"kind": "course_update", "notice_id": row.id,
                       "student": {"id": user.id, "name": user.display_name or user.username},
                       "title": row.title, "content": row.content,
                       "date": datetime.fromtimestamp(row.send_at, timezone.utc).date().isoformat()}
            result = providers.send_message(row.channel, target["target"], title=row.title,
                                            text=row.content, guardian_id=row.user_id, report=payload)
            row.status = ("simulated" if row.channel == "mock" else "sent") if result.ok else "failed"
            row.message_id, row.error = result.message_id, result.error[:300]
        except official.DeliveryUncertain as err:
            row.status, row.error = "unknown", str(err)[:300]
        except ValueError as err:
            row.status, row.error = "skipped", str(err)[:300]
        except Exception:
            row.status, row.error = "unknown", "发送结果不明，不自动重试，请核对微信接收端"
        row.sent_at = time.time() if row.status in ("sent", "simulated") else 0.0
        db.commit()
        return _view(row)


def run_due() -> list[dict]:
    if not settings.auth_enable or not settings.wechat_assistant_enable or not settings.wechat_course_scheduler_enable:
        return []
    now = time.time()
    with get_session() as db:
        ids = db.scalars(select(WeChatUserNotice.id).where(
            WeChatUserNotice.status == "pending", WeChatUserNotice.send_at <= now,
        ).order_by(WeChatUserNotice.send_at).limit(100)).all()
    return [dispatch(notice_id) for notice_id in ids]
