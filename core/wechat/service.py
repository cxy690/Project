"""家长日报：预览、单条发送、当日批量发送与发送记录。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy import select

from config.settings import settings
from core.wechat.bindings import get_target
from core.wechat.official import configuration
from core.wechat.providers import ALLOWED_CHANNELS, channel_status, send_message
from core.wechat.report import build_daily_report, reports_for_date
from db.base import get_session
from db.models import GuardianLink, User, WeChatDelivery, WeChatTarget, account_role

_STATUS_LABELS = {"sent": "已发送", "failed": "发送失败", "skipped": "已跳过", "pending": "待发送"}


def today() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(settings.wechat_report_timezone or "Asia/Shanghai")).date().isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now().date().isoformat()


def _user(user_id: str) -> dict[str, Any] | None:
    with get_session() as db:
        row = db.get(User, (user_id or "").strip())
        if row is None:
            return None
        return {
            "id": row.id,
            "username": row.username,
            "display_name": row.display_name or row.username,
            "role": account_role(row.role),
            "is_active": bool(row.is_active),
            "grade": row.grade,
            "stage": row.stage,
        }


def _first_guardian(student_id: str) -> dict[str, Any] | None:
    with get_session() as db:
        link = db.scalar(
            select(GuardianLink).where(GuardianLink.student_id == student_id).limit(1)
        )
        if link is None:
            return None
        return _user(link.guardian_id)


def preview_daily_report(student_id: str, date: str = "") -> dict[str, Any]:
    """管理台预览；不发送、不写发送记录。"""
    sid = (student_id or "").strip()
    if not sid:
        raise ValueError("student_id 不能为空")
    student = _user(sid)
    if student is None or account_role(student.get("role")) != "user":
        raise ValueError("请选择有效的用户账号")
    guardian = student
    day = (date or today()).strip()
    reports = reports_for_date(sid, day)
    payload = build_daily_report(
        student=student, guardian=guardian, date=day, reports=reports
    )
    payload["ok"] = True
    payload["preview"] = True
    payload["has_course"] = bool(reports)
    return payload


def _save_delivery(
    *,
    day: str,
    guardian_id: str,
    student_id: str,
    channel: str,
    status: str,
    message_id: str,
    error: str,
    text: str,
) -> None:
    with get_session() as db:
        row = db.scalar(
            select(WeChatDelivery).where(
                WeChatDelivery.report_date == day,
                WeChatDelivery.guardian_id == guardian_id,
                WeChatDelivery.student_id == student_id,
            )
        )
        if row is None:
            row = WeChatDelivery(
                report_date=day,
                guardian_id=guardian_id,
                student_id=student_id,
            )
            db.add(row)
        row.channel = channel
        row.status = status
        row.message_id = message_id
        row.error = error
        row.payload = text[:5000]
        row.sent_at = time.time() if status == "sent" else 0.0
        db.commit()


def send_daily_report(
    guardian_id: str, student_id: str, date: str = "", *, force: bool = False
) -> dict[str, Any]:
    """给一位家长发送一个孩子某天的日报；同一天幂等（force=True 可重发）。"""
    gid = (guardian_id or "").strip()
    sid = (student_id or "").strip()
    day = (date or today()).strip()
    if not gid or not sid:
        raise ValueError("guardian_id 和 student_id 不能为空")
    if not settings.wechat_assistant_enable:
        raise ValueError("微信助手已关闭")
    target = get_target(gid)
    channel = str((target or {}).get("channel") or settings.wechat_provider)
    with get_session() as db:
        link = db.scalar(
            select(GuardianLink).where(
                GuardianLink.guardian_id == gid,
                GuardianLink.student_id == sid,
            )
        )
        if gid != sid and link is None:
            raise ValueError("接收账号未获得该学习数据的授权")
        existing = db.scalar(
            select(WeChatDelivery).where(
                WeChatDelivery.report_date == day,
                WeChatDelivery.guardian_id == gid,
                WeChatDelivery.student_id == sid,
            )
        )
        mock_to_real = existing is not None and existing.channel == "mock" and channel != "mock"
        if existing is not None and existing.status == "sent" and not force and not mock_to_real:
            return {
                "ok": True,
                "skipped": True,
                "reason": "今天已经发送过",
                "delivery_id": existing.id,
                "status": "sent",
            }

    student = _user(sid)
    guardian = _user(gid)
    if (student is None or guardian is None or not student.get("is_active") or not guardian.get("is_active")
            or account_role(student.get("role")) != "user"):
        raise ValueError("接收用户或学习账号不存在或已停用")
    reports = reports_for_date(sid, day)
    if not reports:
        _save_delivery(
            day=day, guardian_id=gid, student_id=sid, channel="", status="skipped",
            message_id="", error="当天没有已结束课程", text="",
        )
        return {"ok": True, "skipped": True, "reason": "当天没有已结束课程"}

    payload = build_daily_report(student=student, guardian=guardian, date=day, reports=reports)
    target = get_target(gid)
    channel = str((target or {}).get("channel") or settings.wechat_provider)
    if target is None or not target.get("enabled"):
        _save_delivery(
            day=day, guardian_id=gid, student_id=sid, channel="", status="skipped",
            message_id="", error="用户尚未绑定微信或通知已暂停", text=payload["text"],
        )
        return {"ok": True, "skipped": True, "reason": "用户尚未绑定微信或通知已暂停"}

    result = send_message(
        str(target.get("channel") or settings.wechat_provider),
        str(target.get("target") or ""),
        title=payload["title"],
        text=payload["text"],
        guardian_id=gid,
        report=payload,
    )
    _save_delivery(
        day=day,
        guardian_id=gid,
        student_id=sid,
        channel=str(target.get("channel") or settings.wechat_provider),
        status="sent" if result.ok else "failed",
        message_id=result.message_id,
        error=result.error,
        text=payload["text"],
    )
    return {
        "ok": result.ok,
        "skipped": False,
        "simulated": channel == "mock",
        "channel": channel,
        "status": "sent" if result.ok else "failed",
        "message_id": result.message_id,
        "error": result.error,
        "text": payload["text"],
        "report": payload["report"],
    }


def send_all_today(date: str = "", *, force: bool = False) -> list[dict[str, Any]]:
    """当天所有「已绑定家长 + 有已结束课程」的日报；失败不影响其他家长。"""
    day = (date or today()).strip()
    with get_session() as db:
        links = db.scalars(select(GuardianLink)).all()
        pairs = {(str(item.guardian_id), str(item.student_id)) for item in links}
        pairs.update((str(uid), str(uid)) for uid in db.scalars(select(WeChatTarget.guardian_id)).all())
    results = []
    for guardian_id, student_id in pairs:
        try:
            result = send_daily_report(guardian_id, student_id, day, force=force)
            result["guardian_id"] = guardian_id
            result["student_id"] = student_id
            results.append(result)
        except Exception as err:  # noqa: BLE001 - 一个家长失败不能拖垮整批
            results.append({
                "ok": False,
                "skipped": False,
                "status": "failed",
                "error": str(err),
                "guardian_id": guardian_id,
                "student_id": student_id,
            })
    return results


def list_deliveries(date: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
    day = (date or today()).strip()
    cap = max(1, min(int(limit or 100), 500))
    with get_session() as db:
        rows = db.scalars(
            select(WeChatDelivery)
            .where(WeChatDelivery.report_date == day)
            .order_by(WeChatDelivery.created_at.desc())
            .limit(cap)
        ).all()
        out = []
        for row in rows:
            guardian = db.get(User, row.guardian_id)
            student = db.get(User, row.student_id)
            out.append({
                "id": row.id,
                "date": row.report_date,
                "guardian_id": row.guardian_id,
                "student_id": row.student_id,
                "guardian": (guardian.display_name or guardian.username) if guardian else row.guardian_id,
                "student": (student.display_name or student.username) if student else row.student_id,
                "channel": row.channel,
                "status": row.status,
                "status_label": ("模拟成功（未发微信）" if row.channel == "mock" else "渠道已受理")
                if row.status == "sent" else _STATUS_LABELS.get(row.status, row.status),
                "message_id": row.message_id,
                "error": row.error,
                "created_at": row.created_at,
                "sent_at": row.sent_at,
            })
        return out


def send_test_notification(guardian_id: str) -> dict[str, Any]:
    """发送不含学情的测试通知，不占用日报的发送记录及幂等键。"""
    if not settings.wechat_assistant_enable:
        raise ValueError("微信助手已关闭")
    guardian = _user(guardian_id)
    if guardian is None or not guardian.get("is_active") or guardian.get("role") != "user":
        raise ValueError("请选择有效的用户账号")
    target = get_target(guardian_id)
    if target is None or not target.get("enabled"):
        raise ValueError("请先保存并启用该家长的通知目标")
    channel = str(target.get("channel") or settings.wechat_provider)
    if channel == "official_account":
        raise ValueError("服务号只发送已关联孩子的真实学情，请使用日报预览和手动发送，不发送伪造学情测试")
    state = channel_status(channel, str(target.get("target") or ""))
    if not state["configured"]:
        raise ValueError(state["error"])
    result = send_message(
        channel, str(target.get("target") or ""),
        title="微信助手连接测试",
        text="这是一条微信助手测试通知，不含学生学情。收到此消息表示当前接收目标可用。",
    )
    simulated = state["simulated"]
    return {
        "ok": result.ok, "channel": channel, "simulated": simulated,
        "message_id": result.message_id, "error": result.error,
        "message": ("模拟成功，仅写本地 outbox，未发送微信" if simulated else
                    "渠道已受理测试通知，请在接收端确认；这不代表微信已送达") if result.ok else result.error,
    }


def status_summary(date: str = "") -> dict[str, Any]:
    day = (date or today()).strip()
    with get_session() as db:
        rows = db.scalars(
            select(WeChatDelivery).where(WeChatDelivery.report_date == day)
        ).all()
        return {
            "date": day,
            "bindings": len(db.scalars(select(WeChatTarget)).all()),
            "official": configuration(),
            "course_scheduler_enable": bool(settings.wechat_course_scheduler_enable),
            "sent": sum(1 for row in rows if row.status == "sent"),
            "real_sent": sum(1 for row in rows if row.status == "sent" and row.channel != "mock"),
            "simulated": sum(1 for row in rows if row.status == "sent" and row.channel == "mock"),
            "failed": sum(1 for row in rows if row.status == "failed"),
            "skipped": sum(1 for row in rows if row.status == "skipped"),
            "provider": settings.wechat_provider,
            "channels": [channel_status(ch) for ch in ALLOWED_CHANNELS],
            "assistant_enable": bool(settings.wechat_assistant_enable),
            "scheduler_enable": bool(settings.wechat_scheduler_enable),
            "public_base_url_configured": bool((settings.wechat_public_base_url or "").strip()),
            "report_time": settings.wechat_report_time,
            "report_timezone": settings.wechat_report_timezone,
        }
