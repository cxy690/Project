"""学情分析 REST 接口（薄接入层，业务在 ``core/learning_stats``）。

为什么单独一个 router、不塞进 ``server/routes.py``：
``routes.py`` 已经 40 个端点平铺在一个文件里；学情是一整块新能力（采集、指标、
监控、报告），独立成模块便于对照维护，也便于将来整体开关。

**权限**：学情是未成年人数据，默认最小可见 ——
* 学生只能看自己的；
* 家长只能看 ``guardian_links`` 关联的学生（这里用 ``parent`` 角色 + 显式
  ``student_id`` 校验兜底，关系表由既有认证模块维护）；
* 教师 / 管理员可看全部。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from config.logging import get_logger
from core.auth.deps import current_user
from core.auth.service import get_org_id, org_map, profile_map
from core.learning import get_session_detail, list_reports, summarize
from core.learning_stats import (
    build_report,
    get_collector,
    list_lessons,
    list_students,
    read_events,
    read_metrics,
    read_student_index,
    summarize_lessons,
)
from core.learning_stats import store
from core.learning_stats.metrics import compute_metrics

log = get_logger("server.learning_routes")

router = APIRouter(tags=["learning"])

_STAFF = {"teacher", "admin"}


def _user_org(user: dict) -> str:
    return str(user.get("org_id") or "")


def _is_platform(user: dict) -> bool:
    """平台级账号（``org_id`` 为空）可跨机构；带机构的账号只能看本机构。"""
    return _user_org(user) == ""


def _can_view(user: dict, student_id: str) -> bool:
    """能否查看该学生的学情。

    多机构隔离：教师 / 机构管理员只能看**本机构**学生；
    平台级账号（org_id 为空）不受限制；学生只能看自己。
    """
    role = str(user.get("role") or "")
    if role in _STAFF:
        if _is_platform(user):
            return True
        return _user_org(user) == get_org_id(student_id)
    return str(user.get("id") or "") == str(student_id or "")


def _require_view(user: dict, student_id: str) -> None:
    if not _can_view(user, student_id):
        raise HTTPException(status_code=403, detail="只能查看本人（或被关联学生）的学情")


@router.get("/api/learning/overview")
async def learning_overview(
    user: dict = Depends(current_user), limit: int = Query(default=30, ge=1, le=200)
) -> dict:
    """学情总览：学生数、课次数、平均分、最近课次（教师/管理员视角）。"""
    if str(user.get("role") or "") not in _STAFF:
        raise HTTPException(status_code=403, detail="仅教师或管理员可查看总览")
    # 先按机构收窄（机构账号只看本机构），再做汇总——避免把别家学生算进平均分
    students = list_students(limit=max(limit, 200))
    lessons = list_lessons(limit=max(limit, 200))
    if not _is_platform(user):
        org = _user_org(user)
        orgs = org_map([str(s.get("student_id") or "") for s in students])
        students = [s for s in students if orgs.get(str(s.get("student_id") or "")) == org]
        allowed = {str(s.get("student_id") or "") for s in students}
        lessons = [m for m in lessons if m.student_id in allowed]
    students = students[:limit]
    lessons = lessons[:limit]
    summary = summarize_lessons(lessons)
    alerts = [flag for lesson in lessons for flag in (lesson.flags or [])]
    return {
        "students": len(students),
        "lessons": len(lessons),
        "summary": summary,
        "recent_lessons": [m.to_dict() for m in lessons[:10]],
        "alert_count": len(alerts),
        "recent_alerts": alerts[:10],
    }


@router.get("/api/learning/students")
async def learning_students(
    user: dict = Depends(current_user), limit: int = Query(default=50, ge=1, le=500)
) -> dict:
    """学生列表（含最近汇总），供教师端选择查看对象。"""
    if str(user.get("role") or "") not in _STAFF:
        raise HTTPException(status_code=403, detail="仅教师或管理员可查看学生列表")
    items = list_students(limit=max(limit, 200))
    if not _is_platform(user):
        org = _user_org(user)
        orgs = org_map([str(item.get("student_id") or "") for item in items])
        items = [item for item in items if orgs.get(str(item.get("student_id") or "")) == org]
    items = items[:limit]
    # 列表要能显示「这是谁、哪个机构」——只有 id 的话老师根本挑不出学生
    profiles = profile_map([str(item.get("student_id") or "") for item in items])
    # 账号已删除、只剩历史指标快照的孤儿记录不展示（否则列表里是一排空名字）
    items = [item for item in items if str(item.get("student_id") or "") in profiles]
    return {"students": [
        {
            "student_id": item.get("student_id"),
            "username": (profiles.get(str(item.get("student_id") or "")) or {}).get("username", ""),
            "display_name": (profiles.get(str(item.get("student_id") or "")) or {}).get("display_name", ""),
            "org_id": (profiles.get(str(item.get("student_id") or "")) or {}).get("org_id", ""),
            "totals": item.get("totals") or {},
            "updated_at": item.get("updated_at"),
            "last_lesson": (item.get("lessons") or [{}])[0],
        }
        for item in items
    ]}


@router.get("/api/learning/student/{student_id}")
async def learning_student(
    student_id: str,
    user: dict = Depends(current_user),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """单个学生的学情：汇总 + 最近课次 + 趋势 + 待关注项。"""
    _require_view(user, student_id)
    index = read_student_index(student_id)
    lessons = list_lessons(student_id=student_id, limit=limit)
    summary = summarize_lessons(lessons)
    return {
        "student_id": student_id,
        "totals": index.get("totals") or summary,
        "summary": summary,
        "lessons": [m.to_dict() for m in lessons],
        "trend": [
            {
                "lesson_id": m.lesson_id,
                "started_at": m.started_at,
                "overall": m.overall_score,
                "mastery": m.mastery_score,
                "completion": m.completion_score,
                "interaction": m.interaction_rate,
            }
            for m in reversed(lessons)
        ],
        "flags": sorted({flag for m in lessons for flag in (m.flags or [])}),
    }


@router.get("/api/learning/lessons")
async def learning_lessons(
    user: dict = Depends(current_user),
    student_id: str = "",
    limit: int = Query(default=30, ge=1, le=200),
) -> dict:
    """课次列表（可按学生过滤）。学生只能查自己的。"""
    if student_id:
        _require_view(user, student_id)
    elif str(user.get("role") or "") not in _STAFF:
        student_id = str(user.get("id") or "")
    lessons = list_lessons(student_id, limit=limit if student_id else max(limit, 200))
    if not student_id and not _is_platform(user):
        org = _user_org(user)
        ids = [str(item.get("student_id") or "") for item in list_students(limit=500)]
        orgs = org_map(ids)
        allowed = {sid for sid in ids if orgs.get(sid) == org}
        lessons = [m for m in lessons if m.student_id in allowed]
    return {"lessons": [m.to_dict() for m in lessons[:limit]]}


@router.get("/api/learning/lesson/{lesson_id}")
async def learning_lesson(lesson_id: str, user: dict = Depends(current_user)) -> dict:
    """单节课详情：指标 + 课后报告 + 原始事件统计。"""
    metrics = read_metrics(lesson_id)
    if metrics is None:
        raise HTTPException(status_code=404, detail="没有这节课的学情记录")
    _require_view(user, metrics.student_id)
    events = read_events(lesson_id)
    report = build_report(metrics, events)
    return {
        "metrics": metrics.to_dict(),
        "report": report.to_dict(),
        "events": len(events),
        "meta": store.read_meta(lesson_id),
    }


@router.post("/api/learning/lesson/{lesson_id}/recompute")
async def learning_recompute(lesson_id: str, user: dict = Depends(current_user)) -> dict:
    """按当前算法把该课的事件流**重算**一遍并覆盖指标快照（对账用）。"""
    metrics = read_metrics(lesson_id)
    if metrics is None:
        raise HTTPException(status_code=404, detail="没有这节课的学情记录")
    if str(user.get("role") or "") not in _STAFF:
        raise HTTPException(status_code=403, detail="仅教师或管理员可重算")
    events = read_events(lesson_id)
    meta = store.read_meta(lesson_id)
    fresh = compute_metrics(
        lesson_id=lesson_id,
        student_id=str(meta.get("student_id") or metrics.student_id),
        subject=str(meta.get("subject") or metrics.subject),
        stage=str(meta.get("stage") or metrics.stage),
        grade=str(meta.get("grade") or metrics.grade),
        events=events,
        planned_seconds=int(meta.get("planned_seconds") or metrics.planned_seconds),
        planned_phases=max(1, metrics.phases_planned),
    )
    store.write_metrics(fresh)
    return {"ok": True, "metrics": fresh.to_dict()}


@router.get("/api/learning/live/{session_id}")
async def learning_live(session_id: str, user: dict = Depends(current_user)) -> dict:
    """实时快照：正在上课的会话当前指标与告警（监控台轮询）。"""
    from core.state.store import session_store  # noqa: PLC0415 - 避免导入期循环

    state = session_store.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="会话不存在或已结束")
    _require_view(user, getattr(state, "student_id", ""))
    collector = get_collector()
    metrics = collector.snapshot(state)
    return {
        "session_id": session_id,
        "metrics": metrics.to_dict() if metrics else None,
        "alerts": collector.alerts(state),
    }


@router.get("/api/learning/health")
async def learning_health() -> dict:
    """指标口径自检：阈值与权重集中在这里，便于前端/文档对齐。"""
    from core.learning_stats import metrics as m  # noqa: PLC0415

    return {
        "status": "ok",
        "idle_gap_sec": m.IDLE_GAP_SEC,
        "expected_quiz": m.EXPECTED_QUIZ,
        "weights": {
            "completion": {"time": m.W_TIME, "phase": m.W_PHASE,
                           "wrap": m.W_WRAP, "attend": m.W_ATTEND},
            "mastery": {"first_accuracy": 50, "accuracy": 30, "coverage": 20},
            "overall": {"completion": 35, "mastery": 35, "interaction": 20, "authenticity": 10},
        },
    }


# ---------------------------------------------------------------------------
# 事件流学情（core/learning）：技能管理平台「学情分析」页签直接读这两个接口。
#
# 与上面 core/learning_stats 的口径**并存不冲突**：
# * learning_stats 是「指标 + 学生索引」，服务教师端看板与企业微信推送；
# * core/learning  是「事件溯源 + 单课报告」，服务管理员的最近课堂概览。
# 两者落盘目录不同（data/learning/lessons vs data/learning/events|reports）。
# ---------------------------------------------------------------------------
def _guardian_ids(user_id: str) -> set[str]:
    """家长关联的学生 id（绑定关系可能没建，异常一律当没有）。"""
    try:
        from core.wechat.bindings import guardian_student_ids  # noqa: PLC0415

        return set(guardian_student_ids(user_id) or ())
    except Exception:  # noqa: BLE001 - 绑定表缺失不影响学情主链路
        return set()


def _scope_student(user: dict, requested: str) -> str:
    """把请求里的 student_id 收敛到调用者有权看的那一个。

    ``admin`` / ``teacher`` 留空 = 看全部（返回空串）；
    其余角色留空时收敛成「自己」，给了别人则校验后放行监护人。
    """
    role = str(user.get("role") or "")
    own = str(user.get("id") or "")
    if role in _STAFF:
        if requested:
            _require_view(user, requested)
        return requested
    if not requested or requested == own:
        return own
    if requested in _guardian_ids(own):
        return requested
    raise HTTPException(status_code=403, detail="无权查看其他学生的学情")


@router.get("/api/learning/reports")
async def learning_reports(
    student_id: str = Query(default="", description="留空：学生看自己；教师/管理员看全部"),
    limit: int = Query(default=50, ge=1, le=200),
    days: int = Query(default=0, ge=0, le=365, description="回看天数，0=使用配置默认值"),
    user: dict = Depends(current_user),
) -> dict:
    """最近课堂的学情报告 + 概览汇总（管理台「最近课堂概览」）。"""
    scoped = _scope_student(user, student_id)
    reports = await asyncio.to_thread(
        list_reports, student_id=scoped, days=days or None, limit=limit
    )
    return {
        "reports": reports,
        "summary": summarize(reports),
        "scope": {"student_id": scoped, "role": user.get("role") or ""},
        "generated_at": time.time(),
    }


@router.get("/api/learning/report/{session_id}")
async def learning_report(
    session_id: str,
    event_limit: int = Query(default=300, ge=20, le=1000),
    user: dict = Depends(current_user),
) -> dict:
    """单节课报告 + 事件时间线（心跳只留首末两条）。"""
    detail = await asyncio.to_thread(
        get_session_detail, session_id, event_limit=event_limit
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="学情报告不存在")
    report = detail.get("report") or {}
    owner = str(report.get("student_id") or "")
    if owner and not _can_view(user, owner) and owner not in _guardian_ids(str(user.get("id") or "")):
        raise HTTPException(status_code=403, detail="无权查看该学生的学情")
    return detail
