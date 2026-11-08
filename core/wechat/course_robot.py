"""管理员排课与家长群定时通知；配置及排课写入均不触发发送。"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from core.wechat import providers
from db.base import get_session
from db.models import User, WeChatCourseNotice, WeChatCourseRobot, WeChatScheduledCourse


class RobotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    channel: str = "mock"
    webhook: str = Field(default="", max_length=1024)
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    send_times: list[str] = Field(default_factory=lambda: ["08:00", "18:00"], min_length=1, max_length=8)
    weekdays: list[int] = Field(default_factory=lambda: list(range(1, 8)), min_length=1, max_length=7)
    lookahead_hours: int = Field(default=24, ge=1, le=168)
    grace_minutes: int = Field(default=30, ge=1, le=120)

    @field_validator("name")
    @classmethod
    def name_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("机器人名称不能为空")
        return value.strip()

    @field_validator("channel")
    @classmethod
    def channel_valid(cls, value: str) -> str:
        if value not in {"mock", "wecom_webhook"}:
            raise ValueError("群通知仅支持企业微信群机器人或本地模拟")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as err:
            raise ValueError("无效的时区") from err
        return value

    @field_validator("send_times")
    @classmethod
    def times_valid(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in values):
            raise ValueError("通知时间须为 HH:MM，例如 08:00")
        if len(set(values)) != len(values):
            raise ValueError("通知时间不能重复")
        return sorted(values)

    @field_validator("weekdays")
    @classmethod
    def days_valid(cls, values: list[int]) -> list[int]:
        if any(day not in range(1, 8) for day in values) or len(values) != len(set(values)):
            raise ValueError("星期必须为不重复的 1 至 7，1 为周一")
        return sorted(values)


class CourseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    robot_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    student_ids: list[str] = Field(min_length=1, max_length=40)
    starts_at: str = Field(max_length=64)
    ends_at: str = Field(max_length=64)
    teacher: str = Field(default="", max_length=64)
    location: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=300)
    status: str = "scheduled"

    @field_validator("status")
    @classmethod
    def status_valid(cls, value: str) -> str:
        if value not in {"scheduled", "cancelled"}:
            raise ValueError("课程状态只能为已安排或已取消")
        return value


def _robot(db, robot_id: str) -> WeChatCourseRobot:
    row = db.get(WeChatCourseRobot, robot_id)
    if row is None:
        raise ValueError("课程通知机器人不存在")
    return row


def _lock(db, robot_id: str) -> WeChatCourseRobot:
    db.execute(update(WeChatCourseRobot).where(WeChatCourseRobot.id == robot_id)
               .values(enabled=WeChatCourseRobot.enabled))
    return _robot(db, robot_id)


def _view(row: WeChatCourseRobot) -> dict:
    return {"id": row.id, "name": row.name, "channel": row.channel, "enabled": row.enabled,
            "webhook_configured": bool(row.webhook), "timezone": row.timezone,
            "send_times": json.loads(row.send_times), "weekdays": json.loads(row.weekdays),
            "lookahead_hours": row.lookahead_hours, "grace_minutes": row.grace_minutes,
            "updated_at": row.updated_at}


def list_robots() -> dict:
    with get_session() as db:
        rows = db.scalars(select(WeChatCourseRobot).order_by(WeChatCourseRobot.created_at)).all()
        return {"robots": [_view(row) for row in rows],
                "assistant_enabled": bool(settings.wechat_assistant_enable),
                "scheduler_enabled": bool(settings.wechat_course_scheduler_enable),
                "auth_enabled": bool(settings.auth_enable)}


def save_robot(body: RobotInput, robot_id: str = "") -> dict:
    with get_session() as db:
        row = _lock(db, robot_id) if robot_id else WeChatCourseRobot(id=uuid.uuid4().hex)
        webhook = body.webhook.strip() or (row.webhook if robot_id else "")
        if body.channel == "mock":
            webhook = ""
        elif not webhook or not providers.channel_status("wecom_webhook", webhook)["configured"]:
            raise ValueError("请填写该接收群自己的有效企业微信机器人 webhook，不使用全局共享目标")
        for key, value in body.model_dump(exclude={"webhook", "send_times", "weekdays"}).items():
            setattr(row, key, value)
        row.webhook = webhook
        row.send_times = json.dumps(body.send_times)
        row.weekdays = json.dumps(body.weekdays)
        row.updated_at = time.time()
        db.add(row)
        db.commit()
        return _view(row)


def _when(raw: str, zone: ZoneInfo) -> float:
    try:
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None:
            first, second = value.replace(tzinfo=zone, fold=0), value.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError("夏令时切换时段请填写带时区偏移的时间")
            value = first
        return value.timestamp()
    except (ValueError, OverflowError, OSError) as err:
        raise ValueError("课程时间无效；使用 ISO 日期时间，夏令时歧义时需指定偏移") from err


def _course_view(db, row: WeChatScheduledCourse, zone: ZoneInfo) -> dict:
    ids = json.loads(row.student_ids)
    students = db.scalars(select(User).where(User.id.in_(ids))).all()
    names = {student.id: student.display_name or student.username for student in students}
    return {"id": row.id, "robot_id": row.robot_id, "title": row.title,
            "student_ids": ids, "student_names": [names.get(sid, "已移除学生") for sid in ids],
            "starts_at": datetime.fromtimestamp(row.starts_at, zone).isoformat(),
            "ends_at": datetime.fromtimestamp(row.ends_at, zone).isoformat(),
            "teacher": row.teacher, "location": row.location, "note": row.note, "status": row.status}


def list_courses(robot_id: str) -> list[dict]:
    with get_session() as db:
        zone = ZoneInfo(_robot(db, robot_id).timezone)
        rows = db.scalars(select(WeChatScheduledCourse).where(WeChatScheduledCourse.robot_id == robot_id)
                          .order_by(WeChatScheduledCourse.starts_at.desc()).limit(500)).all()
        return [_course_view(db, row, zone) for row in rows]


def save_course(body: CourseInput, course_id: str = "") -> dict:
    with get_session() as db:
        robot = _lock(db, body.robot_id)
        row = db.get(WeChatScheduledCourse, course_id) if course_id else WeChatScheduledCourse(id=uuid.uuid4().hex)
        if row is None or (course_id and row.robot_id != body.robot_id):
            raise ValueError("课程不存在或不属于所选群，请勿跨群移动课程")
        if course_id and row.starts_at <= time.time():
            raise ValueError("已开始的历史课程不能改写，请新建课程")
        if not body.title.strip() or len(set(body.student_ids)) != len(body.student_ids):
            raise ValueError("课程名称不能为空，学生不可重复")
        students = db.scalars(select(User).where(User.id.in_(body.student_ids), User.role.in_(("user", "student", "parent", "teacher")), User.is_active.is_(True))).all()
        if len(students) != len(body.student_ids):
            raise ValueError("只能安排已存在且启用的学生账号")
        zone = ZoneInfo(robot.timezone)
        start, end = _when(body.starts_at, zone), _when(body.ends_at, zone)
        if end <= start or end - start > 86400:
            raise ValueError("结束时间必须晚于开始时间，单节课不可超过 24 小时")
        if start <= time.time():
            raise ValueError("只能安排或调整尚未开始的课程")
        for key in ("robot_id", "teacher", "location", "note", "status"):
            setattr(row, key, getattr(body, key).strip())
        row.title, row.starts_at, row.ends_at = body.title.strip(), start, end
        row.student_ids, row.updated_at = json.dumps(body.student_ids), time.time()
        db.add(row)
        db.commit()
        return _course_view(db, row, zone)


def cancel_course(course_id: str) -> None:
    with get_session() as db:
        row = db.get(WeChatScheduledCourse, course_id)
        if row is None:
            raise ValueError("课程不存在")
        _lock(db, row.robot_id)
        db.refresh(row)
        if row.starts_at <= time.time():
            raise ValueError("已开始的历史课程不能取消")
        row.status, row.updated_at = "cancelled", time.time()
        db.commit()


def _plain(value: str) -> str:
    return re.sub(r"[<>\[\]`*_@#\\]", "", " ".join(value.split()))


def _messages(db, robot: WeChatCourseRobot, slot_at: datetime, current: datetime) -> list[str]:
    lower = max(slot_at.timestamp(), current.timestamp())
    upper = slot_at.timestamp() + robot.lookahead_hours * 3600
    rows = db.scalars(select(WeChatScheduledCourse).where(
        WeChatScheduledCourse.robot_id == robot.id, WeChatScheduledCourse.starts_at >= lower,
        WeChatScheduledCourse.starts_at < upper,
    ).order_by(WeChatScheduledCourse.starts_at, WeChatScheduledCourse.id).limit(501)).all()
    if len(rows) > 500:
        raise ValueError("本时段课程超过 500 节，请缩小通知范围或拆分接收群")
    if not rows:
        return []
    ids = {sid for row in rows for sid in json.loads(row.student_ids)}
    students = db.scalars(select(User).where(User.id.in_(ids), User.role.in_(("user", "student", "parent", "teacher")), User.is_active.is_(True))).all()
    names = {student.id: _plain(student.display_name or student.username) for student in students}
    header = f"【课程安排通知】{_plain(robot.name)}\n{slot_at:%Y-%m-%d %H:%M}（{robot.timezone}）\n"
    footer = "\n请家长按课程时间安排孩子上课。已取消的课程无需参加。"
    messages, text = [], header
    for row in rows:
        attending = [names[sid] for sid in json.loads(row.student_ids) if sid in names]
        if not attending:
            continue
        start = datetime.fromtimestamp(row.starts_at, slot_at.tzinfo)
        end = datetime.fromtimestamp(row.ends_at, slot_at.tzinfo)
        block = (f"\n{'[已取消] ' if row.status == 'cancelled' else ''}{_plain(row.title)}\n"
                 f"上课：{start:%m-%d %H:%M} — {end:%m-%d %H:%M}\n"
                 f"学生：{'、'.join(attending)}\n")
        for label, value in (("老师", row.teacher), ("地点/入口", row.location), ("备注", row.note)):
            if value:
                block += f"{label}：{_plain(value)}\n"
        if len((header + block + footer).encode("utf-8")) > 3500:
            raise ValueError("单节课程通知过长，请减少学生人数、姓名或备注长度")
        if len((text + block + footer).encode("utf-8")) > 3500:
            messages.append(text + footer)
            text = header
        text += block
    if text != header:
        messages.append(text + footer)
    if len(messages) > 18:
        raise ValueError("本时段通知超过 18 条，请缩小通知范围，避免群机器人限频")
    return messages


def preview(robot_id: str, now: datetime | None = None) -> dict:
    with get_session() as db:
        robot = _robot(db, robot_id)
        zone = ZoneInfo(robot.timezone)
        current = (now or datetime.now(timezone.utc)).astimezone(zone)
        for offset in range(8):
            day = current.date() + timedelta(days=offset)
            if day.isoweekday() not in json.loads(robot.weekdays):
                continue
            for slot in json.loads(robot.send_times):
                slot_at = datetime.fromisoformat(f"{day}T{slot}").replace(tzinfo=zone)
                if slot_at >= current:
                    return {"slot_at": slot_at.isoformat(), "messages": _messages(db, robot, slot_at, current),
                            "channel": robot.channel, "enabled": robot.enabled, "timezone": robot.timezone}
    raise ValueError("未找到下一通知时段")


def _dispatch(robot_id: str, slot_at: datetime, current: datetime, clock) -> dict:
    notice_id = uuid.uuid4().hex
    with get_session() as db:
        robot = _lock(db, robot_id)
        if not robot.enabled or robot.updated_at > slot_at.timestamp():
            return {"status": "not_due"}
        existing = db.scalar(select(WeChatCourseNotice).where(
            WeChatCourseNotice.robot_id == robot_id, WeChatCourseNotice.local_date == slot_at.date().isoformat(),
            WeChatCourseNotice.slot == slot_at.strftime("%H:%M"),
        ))
        if existing:
            if existing.status == "sending" and time.time() - existing.created_at > 600:
                existing.status, existing.error = "unknown", "进程中断或超时，可能已发送；不自动补发，请核对群消息"
                db.commit()
            return {"status": "duplicate", "id": existing.id}
        try:
            messages = _messages(db, robot, slot_at, current)
            error = ""
        except ValueError as err:
            messages, error = [], str(err)
        row = WeChatCourseNotice(id=notice_id, robot_id=robot_id, local_date=slot_at.date().isoformat(),
                                slot=slot_at.strftime("%H:%M"), channel=robot.channel,
                                status="failed" if error else "sending" if messages else "skipped",
                                payload=json.dumps(messages, ensure_ascii=False), error=error)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"status": "duplicate"}
        if row.status != "sending":
            return {"status": row.status, "id": notice_id}
    with get_session() as db:
        robot = _lock(db, robot_id)
        current = clock().astimezone(slot_at.tzinfo)
        row = db.get(WeChatCourseNotice, notice_id)
        if not 0 <= current.timestamp() - slot_at.timestamp() <= robot.grace_minutes * 60:
            row.status, row.error = "skipped", "实际发送前已超过通知补发窗口，不发送过期提醒"
        elif (not robot.enabled or robot.updated_at > slot_at.timestamp()
                or not settings.wechat_assistant_enable or not settings.wechat_course_scheduler_enable
                or not settings.auth_enable):
            row.status, row.error = "skipped", "机器人已暂停、配置已变更或定时开关已关闭"
        elif robot.channel == "wecom_webhook" and not robot.webhook:
            row.status, row.error = "failed", "接收群 webhook 未配置，不使用共享兜底目标"
        else:
            try:
                messages = _messages(db, robot, slot_at, current)
                row.payload = json.dumps(messages, ensure_ascii=False)
                row.status = ("simulated" if robot.channel == "mock" else "sent") if messages else "skipped"
            except ValueError as err:
                messages, row.status, row.error = [], "failed", str(err)
            for message in messages:
                try:
                    result = providers.send_message(robot.channel, robot.webhook, title="课程安排通知", text=message)
                except Exception:
                    row.status, row.error = "unknown", "发送过程异常，可能已发送，请核对群消息；不会自动重试"
                    break
                if not result.ok:
                    row.status, row.error = "failed", result.error[:300]
                    break
                row.accepted_parts += 1
        row.finished_at = time.time()
        db.commit()
        return {"id": row.id, "status": row.status, "accepted_parts": row.accepted_parts}


def run_due(now: datetime | None = None) -> list[dict]:
    if not settings.wechat_assistant_enable or not settings.wechat_course_scheduler_enable or not settings.auth_enable:
        return []
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    began = monotonic()

    def clock():
        return current + timedelta(seconds=monotonic() - began) if now is not None else datetime.now(timezone.utc)

    with get_session() as db:
        robots = [_view(row) for row in db.scalars(select(WeChatCourseRobot).where(WeChatCourseRobot.enabled.is_(True))).all()]
    results = []
    for robot in robots:
        local = clock().astimezone(ZoneInfo(robot["timezone"]))
        if local.isoweekday() not in robot["weekdays"]:
            continue
        due = [datetime.fromisoformat(f"{local.date()}T{slot}").replace(tzinfo=local.tzinfo)
               for slot in robot["send_times"]]
        due = [slot for slot in due if 0 <= local.timestamp() - slot.timestamp() <= robot["grace_minutes"] * 60]
        if due:
            results.append(_dispatch(robot["id"], max(due), local, clock))
    return results


def list_notices(robot_id: str) -> list[dict]:
    with get_session() as db:
        _robot(db, robot_id)
        rows = db.scalars(select(WeChatCourseNotice).where(WeChatCourseNotice.robot_id == robot_id)
                          .order_by(WeChatCourseNotice.created_at.desc()).limit(100)).all()
        return [{"id": row.id, "local_date": row.local_date, "slot": row.slot, "channel": row.channel,
                 "status": "unknown" if row.status == "sending" and time.time() - row.created_at > 600 else row.status,
                 "messages": json.loads(row.payload), "accepted_parts": row.accepted_parts,
                 "error": row.error, "created_at": row.created_at, "finished_at": row.finished_at} for row in rows]
