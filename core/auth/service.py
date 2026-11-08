"""用户仓储与认证业务逻辑（MySQL 存储，见 ``db/models.py``）。

只做「身份认证」：注册、登录、按 id 取用户。授权（谁能访问谁的数据）由
调用方（REST 依赖 / WebSocket 端点）基于返回的 ``role`` / ``id`` 决定。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import select

from config.logging import get_logger
from config.stages import normalize_stage, stage_from_grade
from core.auth.security import hash_password, verify_password
from db.base import get_session
from db.models import ROLES, User

log = get_logger("core.auth.service")

MIN_USERNAME = 3
MAX_USERNAME = 64
MIN_PASSWORD = 6
MAX_PASSWORD = 128


class AuthError(Exception):
    """认证 / 注册的业务错误（带 HTTP 语义的 ``code``）。"""

    def __init__(self, message: str, *, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _validate_credentials(username: str, password: str) -> tuple[str, str]:
    username = (username or "").strip()
    if not (MIN_USERNAME <= len(username) <= MAX_USERNAME):
        raise AuthError(f"用户名长度需在 {MIN_USERNAME}-{MAX_USERNAME} 之间")
    if not (MIN_PASSWORD <= len(password or "") <= MAX_PASSWORD):
        raise AuthError(f"密码长度需在 {MIN_PASSWORD}-{MAX_PASSWORD} 之间")
    return username, password


def register_user(
    username: str,
    password: str,
    *,
    role: str = "student",
    display_name: str = "",
    grade: str = "",
    stage: str = "",
    org_id: str = "",
) -> dict:
    """注册新用户并返回公开视图。用户名重复 -> ``AuthError(409)``。

    ``stage`` 是注册时选择的学段（小学 / 初中），入库统一存 ``primary`` / ``junior``。
    未选时按 ``grade`` 推导；显式传了无法识别的值则拒收（避免静默落到错误学段）。
    ``org_id`` 是多机构隔离的归属键，公开注册一律为空（平台级账号）。
    """
    username, password = _validate_credentials(username, password)
    role = (role or "student").strip().lower()
    if role not in ROLES:
        raise AuthError(f"角色不合法：{role}")

    raw_stage = str(stage or "").strip()
    resolved_stage = normalize_stage(raw_stage)
    if raw_stage and not resolved_stage:
        raise AuthError("学段不合法：请选择小学或初中")
    if not resolved_stage:
        # 兼容老前端：只填了年级（如「初一」「三年级」）时自动推导学段
        resolved_stage = stage_from_grade(grade)

    with get_session() as db:
        if db.scalar(select(User).where(User.username == username)) is not None:
            raise AuthError("用户名已被占用", code=409)
        user = User(
            id=uuid.uuid4().hex,
            username=username,
            password_hash=hash_password(password),
            role=role,
            display_name=(display_name or username).strip(),
            grade=(grade or "").strip(),
            stage=resolved_stage,
            org_id=(org_id or "").strip(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        log.info("新用户注册：{}（{}）", user.username, user.role)
        return user.to_public()


def get_org_id(user_id: str) -> str:
    """取用户所属机构；用户不存在或未归属返回空串。"""
    if not str(user_id or "").strip():
        return ""
    with get_session() as db:
        org = db.scalar(select(User.org_id).where(User.id == str(user_id)))
    return str(org or "")


def org_map(user_ids: list[str]) -> dict[str, str]:
    """批量取机构（列表页过滤用，避免逐个查库）。"""
    ids = [str(i) for i in user_ids if str(i or "").strip()]
    if not ids:
        return {}
    with get_session() as db:
        rows = db.execute(select(User.id, User.org_id).where(User.id.in_(ids))).all()
    return {str(row[0]): str(row[1] or "") for row in rows}


def profile_map(user_ids: list[str]) -> dict[str, dict]:
    """批量取用户展示信息（学情列表要显示昵称与机构，避免前端只看到一串 id）。"""
    ids = [str(i) for i in user_ids if str(i or "").strip()]
    if not ids:
        return {}
    with get_session() as db:
        rows = db.execute(
            select(User.id, User.username, User.display_name, User.org_id).where(User.id.in_(ids))
        ).all()
    return {
        str(row[0]): {
            "username": str(row[1] or ""),
            "display_name": str(row[2] or ""),
            "org_id": str(row[3] or ""),
        }
        for row in rows
    }


def authenticate(username: str, password: str) -> dict:
    """校验账号密码并返回公开视图。失败 -> ``AuthError(404/401/403)``。

    刻意区分「账号不存在」(404) 与「密码不正确」(401)：登录页要给出可操作的提示，
    否则用户永远只看到一句「用户名或密码错误」，分不清是自己记错了账号还是密码。
    代价是允许枚举用户名；本系统是封闭的教学平台，产品明确要求这个体验。
    """
    username = (username or "").strip()
    with get_session() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            raise AuthError("账号不存在，请检查用户名或先注册", code=404)
        if not verify_password(password or "", user.password_hash):
            raise AuthError("密码不正确，请重新输入", code=401)
        if not user.is_active:
            raise AuthError("账号已被停用", code=403)
        # 老账号没有 stage：登录时按 grade 补一次并落库，避免每次上课都靠推导。
        if not normalize_stage(user.stage):
            derived = stage_from_grade(user.grade)
            if derived:
                user.stage = derived
        user.last_login_at = time.time()
        db.commit()
        db.refresh(user)
        return user.to_public()


def get_user(user_id: str) -> dict | None:
    """按 id 取用户公开视图。"""
    if not user_id:
        return None
    with get_session() as db:
        user = db.get(User, user_id)
        return user.to_public() if user is not None else None


def update_user_stage(user_id: str, stage: str) -> dict:
    """用户中心切换学段；只接受 ``primary`` / ``junior``（或「小学」「初中」）。

    切换只影响**之后新进入**的课堂：当前已建立的会话仍持有旧学段，
    避免讲到一半老师人格/语速突然变化。``users.stage`` 立即落库。
    """
    resolved = normalize_stage(stage)
    if not resolved:
        raise AuthError("学段不合法：请选择小学或初中")
    with get_session() as db:
        user = db.get(User, user_id)
        if user is None:
            raise AuthError("用户不存在", code=404)
        if not user.is_active:
            raise AuthError("账号已被停用", code=403)
        user.stage = resolved
        db.commit()
        db.refresh(user)
        log.info("用户切换学段：{} -> {}", user.username, resolved)
        return user.to_public()


def seed_demo_users() -> list[str]:
    """预置演示账号（幂等）：student / parent / teacher / admin，密码均为 123456。"""
    accounts = (
        ("student", "student", "小明同学", "初一", "junior"),
        ("parent", "parent", "小明家长", "", ""),
        ("teacher", "teacher", "王老师", "", ""),
        ("admin", "admin", "管理员", "", ""),
    )
    created: list[str] = []
    for username, role, display_name, grade, stage in accounts:
        try:
            register_user(
                username,
                "123456",
                role=role,
                display_name=display_name,
                grade=grade,
                stage=stage,
            )
            created.append(username)
        except AuthError as err:
            if err.code != 409:  # 已存在则跳过
                raise
    return created
