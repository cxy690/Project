"""ORM 模型：平台用户 / 监护关系 + 学生 / 会话 / 消息 / 错题本。

两个来源合并为一个模型集：
* ``User`` / ``GuardianLink``（A 线）：平台身份体系（core/auth 使用），
  ``student_id`` 取自 ``users.id``，``to_public()`` 是对外安全视图（不含密码哈希）；
* ``Student`` / ``SessionRecord`` / ``MessageRecord`` / ``WrongBook``（B 线）：答疑会话与
  学情落库（db/runtime_sync.py、db/importer.py、scripts/pull_chat_to_mysql.py 使用）。

两套表名不冲突（users / guardian_links / students / sessions / messages / wrong_book），
``Base.metadata.create_all`` 会一次性建全。
"""
from __future__ import annotations

import time

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

#: 平台角色（存储值）。``user`` 是「普通账号」的归一键：注册时未指明身份即落在它上面，
#: 与 ``student`` / ``parent`` / ``teacher`` 同属「非管理员」；``admin`` 是特权角色。
#: 保留 student/parent/teacher 三个细分值是为了学情可见范围
#: （教师按机构、家长按监护关系、学生只看自己），见 server/learning_routes.py。
ROLES: tuple[str, ...] = ("user", "student", "parent", "teacher", "admin")
USER_ROLES: tuple[str, ...] = ("user", "student", "parent", "teacher")


def account_role(role: str) -> str:
    return "user" if role in USER_ROLES else role


def _now() -> float:
    return time.time()


class User(Base):
    """平台用户。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    grade: Mapped[str] = mapped_column(String(32), default="")
    # 学段（primary / junior）：注册时选择，决定数字人老师加载哪套学科技能。
    # 只在 users 表存规范 key，中文展示由 config/stages.py 统一映射。
    stage: Mapped[str] = mapped_column(String(16), default="")
    #: 所属机构（多机构隔离的唯一边界）。空串 = 未归属 / 平台级账号。
    #: 教师、机构管理员只能看本机构学生的学情；平台级（org_id 为空）可跨机构。
    org_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    last_login_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    def to_public(self) -> dict:
        """对外安全视图（不含密码哈希）。

        注意 ``role`` 原样返回：前端按 student/parent/teacher/admin 区分入口，
        不能用 :func:`account_role` 归一成 ``user``，否则家长/教师入口会失效。
        """
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name or self.username,
            "grade": self.grade,
            "stage": self.stage,
            "org_id": self.org_id or "",
            "is_active": bool(self.is_active),
            "created_at": self.created_at,
        }


class GuardianLink(Base):
    """家长 ↔ 学生 监护关系（未成年人数据授权的基础）。"""

    __tablename__ = "guardian_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guardian_id: Mapped[str] = mapped_column(String(64), index=True)
    student_id: Mapped[str] = mapped_column(String(64), index=True)
    relation: Mapped[str] = mapped_column(String(16), default="parent")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class WeChatTarget(Base):
    """家长微信通知目标（一位家长一条，渠道 + 目标地址/密钥）。

    ``target`` 可能是 webhook URL 或 SendKey，接口返回时必须脱敏，不能原样回显。
    """

    __tablename__ = "wechat_targets"
    __table_args__ = (UniqueConstraint("guardian_id", name="uq_wechat_target_guardian"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guardian_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="mock")
    target: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class WeChatIdentity(Base):
    """家长授权的服务号身份；openid 不从客户端接收。"""

    __tablename__ = "wechat_identities"
    __table_args__ = (UniqueConstraint("app_id", "openid", name="uq_wechat_identity_openid"),)

    guardian_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64))
    openid: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float(53), default=_now)
    updated_at: Mapped[float] = mapped_column(Float(53), default=_now)


class WeChatBindSession(Base):
    """短期、一次性 OAuth 绑定会话，不存平台 JWT 或微信 access_token。"""

    __tablename__ = "wechat_bind_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    guardian_id: Mapped[str] = mapped_column(String(64), index=True)
    app_id: Mapped[str] = mapped_column(String(64))
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default="waiting")
    openid: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[float] = mapped_column(Float(53), index=True)
    created_at: Mapped[float] = mapped_column(Float(53), default=_now)


class WeChatDelivery(Base):
    """家长日报发送记录：按「日期 + 家长 + 学生」幂等。"""

    __tablename__ = "wechat_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "report_date", "guardian_id", "student_id",
            name="uq_wechat_delivery_day_guardian_student",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[str] = mapped_column(String(10), index=True)
    guardian_id: Mapped[str] = mapped_column(String(64), index=True)
    student_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="mock")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    message_id: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    sent_at: Mapped[float] = mapped_column(Float, default=0.0)


class WeChatUserNotice(Base):
    """管理员为指定用户安排的课程更新，发送前唯一领取。"""

    __tablename__ = "wechat_user_notices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    send_at: Mapped[float] = mapped_column(Float(53), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="official_account")
    message_id: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[float] = mapped_column(Float(53), default=_now)
    sent_at: Mapped[float] = mapped_column(Float(53), default=0.0)


class WeChatCourseRobot(Base):
    """管理员配置的家长群课程通知机器人。"""

    __tablename__ = "wechat_course_robots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(32), default="mock")
    webhook: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    send_times: Mapped[str] = mapped_column(Text, default='["08:00", "18:00"]')
    weekdays: Mapped[str] = mapped_column(Text, default="[1,2,3,4,5,6,7]")
    lookahead_hours: Mapped[int] = mapped_column(Integer, default=24)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[float] = mapped_column(Float(53), default=_now)
    updated_at: Mapped[float] = mapped_column(Float(53), default=_now)


class WeChatScheduledCourse(Base):
    """未来课程安排，不启动或扣除实际课时。"""

    __tablename__ = "wechat_scheduled_courses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(80))
    student_ids: Mapped[str] = mapped_column(Text, default="[]")
    starts_at: Mapped[float] = mapped_column(Float(53), index=True)
    ends_at: Mapped[float] = mapped_column(Float(53))
    teacher: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(160), default="")
    note: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    created_at: Mapped[float] = mapped_column(Float(53), default=_now)
    updated_at: Mapped[float] = mapped_column(Float(53), default=_now)


class WeChatCourseNotice(Base):
    """群级时段快照；唯一领取，网络结果不明时不自动重试。"""

    __tablename__ = "wechat_course_notices"
    __table_args__ = (UniqueConstraint("robot_id", "local_date", "slot", name="uq_wechat_course_notice_slot"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(64), index=True)
    local_date: Mapped[str] = mapped_column(String(10))
    slot: Mapped[str] = mapped_column(String(5))
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="sending", index=True)
    payload: Mapped[str] = mapped_column(Text, default="[]")
    accepted_parts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[float] = mapped_column(Float(53), default=_now)
    finished_at: Mapped[float] = mapped_column(Float(53), default=0.0)


class Student(Base):
    """学生。"""

    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    grade: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)

    sessions: Mapped[list["SessionRecord"]] = relationship(back_populates="student")


class SessionRecord(Base):
    """一次答疑会话。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    started_at: Mapped[float] = mapped_column(Float, default=_now)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    student: Mapped[Student] = relationship(back_populates="sessions")
    messages: Mapped[list["MessageRecord"]] = relationship(back_populates="session")


class MessageRecord(Base):
    """会话消息。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[float] = mapped_column(Float, default=_now)

    session: Mapped[SessionRecord] = relationship(back_populates="messages")


class WrongBook(Base):
    """错题本。"""

    __tablename__ = "wrong_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(32), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    analysis: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[float] = mapped_column(Float, default=_now)
