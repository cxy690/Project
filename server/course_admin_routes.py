"""管理员课程监控：查看当前正在进行的 1v1 课堂。

数据源是内存里的 :class:`SessionState`（老师、阶段、出题数、学生回合）与
课时表（已用 / 剩余）。只读，不干预课堂；断线残留的会话通过
``last_active_at`` 过滤，避免把已经离开的课堂一直挂在“进行中”。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from config.settings import settings
from config.stages import stage_label
from core.auth.deps import require_developer
from core.course_billing import get_course_manager
from core.course_session import duration_seconds, phase_label
from core.state.store import session_store

router = APIRouter()


def _state_row(state: Any, now: float) -> dict[str, Any]:
    started = float(getattr(state, "course_started_at", 0.0) or state.created_at)
    deadline = float(
        getattr(state, "course_deadline", 0.0) or (started + duration_seconds())
    )
    student_turns = sum(1 for msg in state.history if getattr(msg, "role", "") == "student")
    ai_turns = sum(1 for msg in state.history if getattr(msg, "role", "") == "ai")
    stage = str((getattr(state, "scratch", {}) or {}).get("stage") or "")
    phase = str(getattr(state, "course_phase", "") or "")
    remaining = max(0.0, deadline - now)
    overtime = remaining <= 0
    return {
        "session_id": state.session_id,
        "student_id": state.student_id,
        "subject": state.subject or "综合",
        "teacher": state.teacher or str((state.scratch or {}).get("teacher") or ""),
        "stage": stage,
        "stage_label": stage_label(stage) if stage else "",
        "grade": str((state.scratch or {}).get("grade") or ""),
        "phase": phase,
        "phase_label": phase_label(phase),
        "started_at": started,
        "deadline": deadline,
        "elapsed_seconds": max(0, int(now - started)),
        "remaining_seconds": int(remaining),
        "quiz_count": int(getattr(state, "quiz_count", 0) or 0),
        "student_turns": student_turns,
        "ai_turns": ai_turns,
        "last_active_at": float(getattr(state, "last_active_at", state.created_at)),
        "status": "overtime" if overtime else "active",
        "status_label": "已超时" if overtime else "进行中",
    }


@router.get("/api/admin/courses")
async def admin_active_courses(user: dict = Depends(require_developer)) -> dict:
    """当前进行中的课堂（仅 admin）。

    只返回仍有心跳/消息活动的会话；老师、阶段、出题数、学生回合都来自
    会话内存，不在浏览器端做二次推断。
    """
    now = time.time()
    # 心跳间隔 15 秒；留 2 个周期余量，短暂网络抖动不把课堂误判为离开。
    timeout = max(60.0, float(settings.learning_heartbeat_timeout_seconds) * 2.0)
    cutoff = now - timeout
    rows: list[dict[str, Any]] = []
    for session_id in session_store.list_ids():
        state = session_store.get(session_id)
        if state is None or not state.course_active:
            continue
        if float(getattr(state, "last_active_at", 0.0) or 0.0) < cutoff:
            continue
        rows.append(_state_row(state, now))
    rows.sort(key=lambda item: (-float(item["started_at"]), str(item["student_id"])))

    try:
        billing = get_course_manager().active_snapshot()
    except Exception:  # noqa: BLE001 - 计费表异常不影响课堂监控
        billing = []

    return {
        "ok": True,
        "courses": rows,
        "billing": billing,
        "summary": {
            "active": len(rows),
            "overtime": sum(1 for row in rows if row["status"] == "overtime"),
            "students": len({row["student_id"] for row in rows}),
        },
        "generated_at": now,
        "operator": user.get("username") or "",
    }
