"""课堂事件采集：把「学情分析」需要的事实写进 append-only 事件流。

采集原则：
* **只采事实，不采大对象**：题干/学生输入截断保存，音频、图像 base64、白板
  指令不落学情库（它们已在别处有缓存）；
* **失败静默**：任何写盘异常都不能影响课堂；
* **不改变业务语义**：这里只旁路记录，不参与答疑决策。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from config.logging import get_logger
from config.settings import settings
from core.learning.store import append_event

log = get_logger("core.learning.events")

_MAX_TEXT = 300


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _session_ids(session: Any) -> tuple[str, str, str, str]:
    if session is None:
        return "", "", "", ""
    scratch = getattr(session, "scratch", {}) or {}
    return (
        str(getattr(session, "session_id", "") or ""),
        str(getattr(session, "student_id", "") or ""),
        str(getattr(session, "subject", "") or scratch.get("subject") or ""),
        str(scratch.get("stage") or ""),
    )


def record_event(
    event_type: str,
    *,
    session: Any | None = None,
    payload: dict[str, Any] | None = None,
    ts: float | None = None,
    session_id: str = "",
    student_id: str = "",
    subject: str = "",
    stage: str = "",
) -> dict[str, Any] | None:
    """写入一条学情事件；关闭学情分析时返回 None。"""
    if not settings.learning_analytics_enable:
        return None
    sid, stu, subj, stg = _session_ids(session)
    event = {
        "version": 1,
        "event_id": uuid.uuid4().hex,
        "ts": float(ts if ts is not None else time.time()),
        "event_type": str(event_type or "").strip(),
        "session_id": str(session_id or sid),
        "student_id": str(student_id or stu),
        "subject": str(subject or subj),
        "stage": str(stage or stg),
        "payload": payload or {},
    }
    if not event["event_type"]:
        return None
    return event if append_event(event) else None


def record_session_start(
    session: Any,
    *,
    planned_seconds: float | None = None,
    resumed: bool = False,
) -> dict[str, Any] | None:
    planned = float(
        planned_seconds
        if planned_seconds is not None
        else max(1, int(settings.course_duration_minutes)) * 60
    )
    scratch = getattr(session, "scratch", {}) or {}
    return record_event(
        "session_start",
        session=session,
        payload={
            "planned_seconds": int(planned),
            "resumed": bool(resumed),
            "grade": str(scratch.get("grade") or ""),
            "teacher": str(getattr(session, "teacher", "") or ""),
            "subject": str(getattr(session, "subject", "") or ""),
            "stage": str(scratch.get("stage") or ""),
        },
    )


def record_heartbeat(session: Any, *, visible: bool = True) -> dict[str, Any] | None:
    return record_event(
        "heartbeat",
        session=session,
        payload={"visible": bool(visible)},
    )


def record_course_start(
    session: Any, *, subject: str = "", teacher: str = ""
) -> dict[str, Any] | None:
    event = record_event(
        "course_start",
        session=session,
        payload={
            "subject": _clip(subject or getattr(session, "subject", ""), 40),
            "teacher": _clip(teacher or getattr(session, "teacher", ""), 40),
        },
    )
    scratch = getattr(session, "scratch", None)
    if isinstance(scratch, dict):
        # course_state 紧随其后到达时不要再写一条 course_start
        scratch["_learning_course_started"] = True
    return event


def record_student_turn(
    session: Any,
    turn: Any,
    *,
    is_answer: bool = False,
    intent: str = "",
    elapsed_ms: float = 0.0,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    actual_intent = intent or getattr(getattr(turn, "intent", None), "value", None) or str(
        getattr(turn, "intent", "") or ""
    )
    return record_event(
        "student_turn",
        session=session,
        payload={
            "text": _clip(getattr(turn, "text", "")),
            "intent": _clip(actual_intent, 40),
            "is_answer": bool(is_answer),
            "image": bool(getattr(turn, "image_id", "")),
            "elapsed_ms": round(float(elapsed_ms), 1),
            "error": type(error).__name__ if error is not None else "",
        },
    )


def observe_session_event(
    session: Any, event_type: str, payload: Any
) -> dict[str, Any] | None:
    """旁路监听 ``SessionState.push_event`` 的事件（只挑学情需要的）。"""
    if not settings.learning_analytics_enable:
        return None
    data = payload if isinstance(payload, dict) else {}
    etype = str(event_type or "").strip()

    if etype == "course_state":
        return _observe_course_state(session, data)

    if etype == "ai_quiz":
        return record_event(
            "ai_quiz",
            session=session,
            payload={
                "qid": _clip(data.get("qid"), 80),
                "type": _clip(data.get("type"), 20),
                "topic": _clip(data.get("topic"), 80),
                "question": _clip(data.get("question")),
                "options": len(data.get("options") or []),
            },
        )

    if etype == "ai_ask":
        return record_event(
            "ai_ask",
            session=session,
            payload={
                "question": _clip(data.get("question")),
                "options": len(data.get("options") or []),
                "index": int(data.get("index") or 0),
            },
        )

    if etype == "ai_check":
        return record_event(
            "ai_check",
            session=session,
            payload={
                "qid": _clip(data.get("qid"), 80),
                "kind": _clip(data.get("kind"), 20),
                "correct": bool(data.get("correct")),
                "allow_retry": bool(data.get("allow_retry")),
                "expected": _clip(data.get("expected"), 120),
            },
        )

    if etype == "crisis_alert":
        return record_event(
            "crisis_alert",
            session=session,
            payload={
                "hits": [str(item)[:40] for item in (data.get("hits") or [])][:6],
                "text": _clip(data.get("text")),
            },
        )
    return None


def _observe_course_state(session: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """course_state 只落「开课 / 阶段变化」，不把每次状态快照都写一遍。"""
    scratch = getattr(session, "scratch", None)
    if not isinstance(scratch, dict):
        return None
    phase = _clip(payload.get("phase"), 20)
    active = bool(payload.get("active"))
    result = None
    if active and not scratch.get("_learning_course_started"):
        result = record_event(
            "course_start",
            session=session,
            payload={
                "duration_seconds": int(payload.get("duration_seconds") or 0),
                "started_at": payload.get("started_at"),
                "deadline": payload.get("deadline"),
                "phase": phase,
            },
        )
        scratch["_learning_course_started"] = True
    if phase and phase != scratch.get("_learning_phase"):
        result = record_event(
            "phase_change",
            session=session,
            payload={"phase": phase, "active": active},
        ) or result
        scratch["_learning_phase"] = phase
    if phase == "ended" and not scratch.get("_learning_course_ended"):
        record_event("course_end", session=session, payload={"reason": "phase_ended"})
        scratch["_learning_course_ended"] = True
    return result


def finish_session(session: Any, reason: str = "disconnect") -> None:
    """会话收尾：记录结束事件并生成/刷新报告。"""
    if session is None or not settings.learning_analytics_enable:
        return
    try:
        record_event("session_end", session=session, payload={"reason": str(reason or "")})
        sid = str(getattr(session, "session_id", "") or "")
        if sid:
            from core.learning.service import get_report  # 延迟导入，避免循环

            get_report(sid, use_cache=False)
    except Exception as err:  # noqa: BLE001 - 收尾失败不能影响断连
        log.warning("学情会话收尾失败：{}", err)
