"""家长 ↔ 学生绑定，以及家长微信通知目标。"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from core.wechat.providers import channel_status
from db.base import get_session
from db.models import GuardianLink, User, WeChatIdentity, WeChatTarget, USER_ROLES, account_role

_ALLOWED_CHANNELS = ("official_account", "mock", "wecom_webhook", "serverchan")


def ensure_guardian_link(guardian_id: str, student_id: str, relation: str = "parent") -> bool:
    """幂等建立监护关系；返回是否新建。"""
    gid = (guardian_id or "").strip()
    sid = (student_id or "").strip()
    if not gid or not sid or gid == sid:
        return False
    with get_session() as db:
        exists = db.scalar(
            select(GuardianLink).where(
                GuardianLink.guardian_id == gid,
                GuardianLink.student_id == sid,
            )
        )
        if exists is not None:
            return False
        db.add(GuardianLink(guardian_id=gid, student_id=sid, relation=relation or "parent"))
        db.commit()
        return True


def guardian_student_ids(guardian_id: str) -> list[str]:
    gid = (guardian_id or "").strip()
    if not gid:
        return []
    with get_session() as db:
        rows = db.scalars(
            select(GuardianLink.student_id).where(GuardianLink.guardian_id == gid)
        ).all()
        return [str(item) for item in rows if str(item or "").strip()]


def student_guardian_ids(student_id: str) -> list[str]:
    sid = (student_id or "").strip()
    if not sid:
        return []
    with get_session() as db:
        rows = db.scalars(
            select(GuardianLink.guardian_id).where(GuardianLink.student_id == sid)
        ).all()
        return [str(item) for item in rows if str(item or "").strip()]


def mask_target(value: str) -> str:
    """列表接口脱敏：不把 webhook / SendKey 原样回显给前端。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "…" + text[-4:]


def _user_view(user: User | None) -> dict[str, Any]:
    if user is None:
        return {}
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": account_role(user.role),
        "grade": user.grade,
        "stage": user.stage,
    }


def list_options() -> dict[str, Any]:
    """普通账号列表与本人微信状态；不返回 openid 或推送密钥。"""
    from config.settings import settings

    with get_session() as db:
        users = db.scalars(select(User).where(User.role.in_(USER_ROLES), User.is_active.is_(True))
                           .order_by(User.username)).all()
        identities = {row.guardian_id: row for row in db.scalars(select(WeChatIdentity)).all()}
        rows = []
        for user in users:
            identity = identities.get(user.id)
            valid = bool(identity and identity.app_id == settings.wechat_official_app_id.strip())
            rows.append({**user.to_public(), "bound": bool(identity), "identity_valid": valid,
                         "notifications_enabled": bool(valid and identity.enabled)})
        return {"users": rows, "parents": rows, "students": rows}


def list_bindings() -> list[dict[str, Any]]:
    with get_session() as db:
        links = db.scalars(select(GuardianLink).order_by(GuardianLink.created_at)).all()
        targets = {
            item.guardian_id: item
            for item in db.scalars(select(WeChatTarget)).all()
        }
        rows: list[dict[str, Any]] = []
        for link in links:
            guardian = db.get(User, link.guardian_id)
            student = db.get(User, link.student_id)
            target = targets.get(link.guardian_id)
            effective = get_target(link.guardian_id)
            channel = str((effective or {}).get("channel") or "")
            config = channel_status(channel, str((effective or {}).get("target") or "")) if effective else None
            if channel == "official_account" and db.get(WeChatIdentity, link.guardian_id) is None:
                config = {"configured": False, "simulated": False, "label": "等待用户在用户中心绑定微信",
                          "source": "authorization", "error": ""}
            rows.append({
                "id": link.id,
                "guardian_id": link.guardian_id,
                "student_id": link.student_id,
                "relation": link.relation,
                "guardian": _user_view(guardian),
                "student": _user_view(student),
                "channel": channel,
                "target_masked": "用户已授权微信身份" if channel == "official_account" and db.get(WeChatIdentity, link.guardian_id)
                else mask_target(target.target) if target else "",
                "target_configured": bool(config and config["configured"]),
                "configuration": config,
                "enabled": bool(effective and effective["enabled"]),
            })
        return rows


def _upsert_target(
    db: Any, guardian_id: str, channel: str, target: str, enabled: bool
) -> WeChatTarget:
    identity = db.get(WeChatIdentity, guardian_id)
    if identity is not None and channel != "official_account":
        raise ValueError("该用户已在用户中心绑定服务号，请保留服务号渠道；更换前需用户自行解绑")
    if channel == "official_account":
        if target:
            raise ValueError("服务号身份必须由用户在用户中心授权，不能手工填写目标")
        if identity is None:
            row = db.scalar(select(WeChatTarget).where(WeChatTarget.guardian_id == guardian_id))
            if row is None:
                row = WeChatTarget(guardian_id=guardian_id)
                db.add(row)
            row.channel, row.target, row.enabled = channel, "", False
            row.updated_at = time.time()
            return row
        row = db.scalar(select(WeChatTarget).where(WeChatTarget.guardian_id == guardian_id))
        if row is None:
            row = WeChatTarget(guardian_id=guardian_id)
            db.add(row)
        row.channel, row.target, row.enabled = channel, "", True
        row.updated_at = time.time()
        return row
    row = db.scalar(select(WeChatTarget).where(WeChatTarget.guardian_id == guardian_id))
    if row is None:
        row = WeChatTarget(guardian_id=guardian_id)
        db.add(row)
    # 同一家长新增第二个孩子时，管理台通常不会重新输入 webhook/SendKey；
    # 渠道相同且目标留空时沿用旧目标，避免把已配置的通知目标清空。
    next_target = (target or "").strip()
    if not next_target and row.target and row.channel == channel:
        next_target = row.target
    state = channel_status(channel, next_target)
    if enabled and not state["configured"]:
        raise ValueError(state["error"])
    row.channel = channel
    row.target = next_target
    row.enabled = bool(enabled)
    row.updated_at = time.time()
    return row


def add_binding(
    *,
    guardian_id: str,
    student_id: str,
    channel: str = "mock",
    target: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """新建/更新绑定。一个家长只保留一个通知目标；可关联多个孩子。"""
    gid = (guardian_id or "").strip()
    sid = (student_id or "").strip()
    ch = (channel or "mock").strip().lower()
    if ch not in _ALLOWED_CHANNELS:
        raise ValueError("不支持的渠道：" + ch)
    if not gid or not sid:
        raise ValueError("guardian_id 和 student_id 不能为空")
    with get_session() as db:
        guardian = db.get(User, gid)
        student = db.get(User, sid)
        if guardian is None or account_role(guardian.role) != "user" or not guardian.is_active:
            raise ValueError("接收账号必须是有效用户")
        if student is None or account_role(student.role) != "user" or not student.is_active:
            raise ValueError("学习账号必须是有效用户")
        link = db.scalar(
            select(GuardianLink).where(
                GuardianLink.guardian_id == gid,
                GuardianLink.student_id == sid,
            )
        )
        if gid != sid:
            if link is None:
                db.add(GuardianLink(guardian_id=gid, student_id=sid, relation="authorized"))
            else:
                link.relation = "authorized"
        _upsert_target(db, gid, ch, (target or "").strip(), enabled)
        db.commit()
    effective = get_target(gid)
    return {
        "ok": True,
        "guardian_id": gid,
        "student_id": sid,
        "channel": ch,
        "target_masked": mask_target(target),
        "enabled": bool(effective and effective["enabled"]),
    }


def delete_binding(guardian_id: str, student_id: str) -> bool:
    gid = (guardian_id or "").strip()
    sid = (student_id or "").strip()
    if not gid or not sid:
        return False
    with get_session() as db:
        row = db.scalar(
            select(GuardianLink).where(
                GuardianLink.guardian_id == gid,
                GuardianLink.student_id == sid,
            )
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


def get_target(guardian_id: str) -> dict[str, Any] | None:
    """内部发送用：返回**未脱敏**目标，调用方不得直接回显。"""
    gid = (guardian_id or "").strip()
    if not gid:
        return None
    with get_session() as db:
        identity = db.get(WeChatIdentity, gid)
        if identity is not None:
            from config.settings import settings

            return {"guardian_id": gid, "channel": "official_account", "target": "",
                    "enabled": bool(identity.enabled and identity.app_id == settings.wechat_official_app_id.strip())}
        row = db.scalar(select(WeChatTarget).where(WeChatTarget.guardian_id == gid))
        if row is None:
            return None
        return {
            "guardian_id": row.guardian_id,
            "channel": row.channel,
            "target": row.target,
            "enabled": bool(row.enabled),
        }
