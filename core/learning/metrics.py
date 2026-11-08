"""学情指标：把事件流聚合成一节课的「课时完成 / 学习掌握 / 互动率」。

三个指标刻意分开，避免把「挂机」误判成「学会」：

* 课时完成度 ``completion_rate``：有效在场、阶段到达、出题作答三个维度；
* 学习掌握度 ``mastery``：只统计**首次作答**正确率，避免重试刷分；
* 互动率 ``interaction_rate``：响应率、主动提问、有效互动三个维度。

所有阈值都是可解释的默认值，后续可按真实数据校准；报告里同时返回
原始计数（``active_seconds`` / ``quiz_issued`` / ``attempts`` 等），便于人工复核。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from config.settings import settings

#: 报告口径版本：改公式/阈值时 +1，旧缓存报告会自动重算
REPORT_VERSION = 1

#: 课程阶段权重：与 config.constants.TUTOR_PHASE_RATIOS 对齐（只影响课时完成度）
PHASE_WEIGHTS: dict[str, float] = {
    "opening": 0.0,
    "lecture": 0.75,
    "interaction": 0.25,
    "summary": 0.0,
}

#: 状态 -> 中文标签
STATUS_LABELS: dict[str, str] = {
    "completed": "完成学习",
    "attended_not_learned": "人在心不在",
    "unverified": "未验证",
    "partial": "部分完成",
    "abandoned": "中途离开",
    "in_progress": "进行中",
}

#: 判定阈值
COMPLETION_PASS = 0.80
MASTERY_PASS = 0.70
MASTERY_LOW = 0.50
INTERACTION_LOW = 0.30
MIN_FIRST_ATTEMPTS = 2
SESSION_ACTIVE_WINDOW = 300  # 无 session_end 时，最后事件在 5 分钟内视为进行中


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload")
    return raw if isinstance(raw, dict) else {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _active_seconds(
    events: list[dict[str, Any]], planned_seconds: float
) -> tuple[float, str]:
    """有效在场时长：只在「可见 + 心跳间隔 ≤ timeout」的时间段累计。"""
    heartbeats = [
        item for item in events if str(item.get("event_type") or "") == "heartbeat"
    ]
    timeout = max(5.0, float(settings.learning_heartbeat_timeout_seconds))
    if len(heartbeats) >= 2:
        active = 0.0
        for prev, cur in zip(heartbeats, heartbeats[1:]):
            prev_ts = _num(prev.get("ts"))
            cur_ts = _num(cur.get("ts"))
            visible = _payload(prev).get("visible")
            if visible is False:
                continue
            gap = cur_ts - prev_ts
            if 0 <= gap <= timeout:
                active += gap
        return min(active, planned_seconds) if planned_seconds > 0 else active, "heartbeat"

    # 老数据 / 未上报心跳：退化为首末事件跨度，明确标注来源，避免假装精确。
    if len(events) >= 2:
        span = max(0.0, _num(events[-1].get("ts")) - _num(events[0].get("ts")))
        return (min(span, planned_seconds) if planned_seconds > 0 else span), "events"
    return 0.0, "heartbeat"


def _phases(events: list[dict[str, Any]]) -> tuple[list[str], bool]:
    phases: OrderedDict[str, None] = OrderedDict()
    for item in events:
        etype = str(item.get("event_type") or "")
        payload = _payload(item)
        if etype in ("phase_change", "course_state", "course_start"):
            phase = str(payload.get("phase") or "").strip()
            if phase:
                phases[phase] = None
    return list(phases.keys()), "summary" in phases


def _quiz_and_attempts(events: list[dict[str, Any]]) -> dict[str, Any]:
    quiz_ids: set[str] = set()
    attempts: list[dict[str, Any]] = []
    for item in events:
        etype = str(item.get("event_type") or "")
        payload = _payload(item)
        if etype == "ai_quiz":
            qid = str(payload.get("qid") or "").strip()
            if qid:
                quiz_ids.add(qid)
        elif etype == "ai_check":
            attempts.append(payload)

    # 每个 prompt 只看第一次作答：重试答对不代表首次掌握。
    first: dict[str, bool] = {}
    answered_quiz: set[str] = set()
    for payload in attempts:
        qid = str(payload.get("qid") or "").strip()
        key = qid or str(payload.get("expected") or "").strip() or f"prompt-{len(first)}"
        if key not in first:
            first[key] = bool(payload.get("correct"))
        if qid:
            answered_quiz.add(qid)

    first_attempts = len(first)
    correct_first = sum(1 for ok in first.values() if ok)
    return {
        "quiz_issued": len(quiz_ids),
        "quiz_answered": len(answered_quiz),
        "attempts": len(attempts),
        "first_attempts": first_attempts,
        "correct_first": correct_first,
        "accuracy": (correct_first / first_attempts) if first_attempts else None,
        "answered_prompts": first_attempts,
    }


def _student_turns(events: list[dict[str, Any]]) -> dict[str, int]:
    turns = 0
    answers = 0
    questions = 0
    images = 0
    for item in events:
        if str(item.get("event_type") or "") != "student_turn":
            continue
        payload = _payload(item)
        turns += 1
        if payload.get("is_answer"):
            answers += 1
        elif str(payload.get("intent") or "") not in ("control", "chitchat", "crisis"):
            questions += 1
        if payload.get("image"):
            images += 1
    return {"turns": turns, "answers": answers, "questions": questions, "images": images}


def _prompt_count(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for item in events
        if str(item.get("event_type") or "") in ("ai_quiz", "ai_ask")
    )


def compute_report(events: list[dict[str, Any]], session_id: str = "") -> dict[str, Any]:
    """把同一会话的事件聚合成报告（纯函数，不落盘）。"""
    events = sorted(
        [item for item in events if isinstance(item, dict)],
        key=lambda item: _num(item.get("ts")),
    )
    sid = session_id or next(
        (str(item.get("session_id") or "") for item in events if item.get("session_id")),
        "",
    )
    if not events:
        return {
            "session_id": sid,
            "status": "abandoned",
            "status_label": STATUS_LABELS["abandoned"],
            "flags": ["no_events"],
        }

    start_payload = next(
        (_payload(item) for item in events if item.get("event_type") == "session_start"),
        {},
    )
    last_end = max(
        (_num(item.get("ts")) for item in events if item.get("event_type") == "session_end"),
        default=0.0,
    )
    last_start = max(
        (_num(item.get("ts")) for item in events if item.get("event_type") == "session_start"),
        default=0.0,
    )
    # 断线重连会先写 session_end 再写新的 session_start：后者更晚则视为仍在进行，
    # 不能因为历史上出现过一次 end 就把整节课判成已结束。
    ended = bool(last_end and last_end >= last_start)

    session_started_at = _num(events[0].get("ts"))
    # 课时从学生真正点「开始上课」算起；开课闸门前的等待时间不能算学习时长。
    course_started_at = next(
        (_num(item.get("ts")) for item in events if item.get("event_type") == "course_start"),
        0.0,
    )
    started_at = course_started_at or session_started_at
    ended_at = _num(events[-1].get("ts"))
    planned = _num(
        start_payload.get("planned_seconds"),
        float(settings.course_duration_minutes) * 60.0,
    )
    if planned <= 0:
        planned = float(settings.course_duration_minutes) * 60.0

    lesson_events = [item for item in events if _num(item.get("ts")) >= started_at] or events
    active, presence_source = _active_seconds(lesson_events, planned)
    phases, summary_reached = _phases(lesson_events)
    phase_ratio = _clip01(sum(PHASE_WEIGHTS.get(phase, 0.0) for phase in phases))

    quiz = _quiz_and_attempts(lesson_events)
    quiz_ratio = (
        _clip01(quiz["quiz_answered"] / quiz["quiz_issued"])
        if quiz["quiz_issued"] > 0
        else 0.0
    )

    time_ratio = _clip01(active / planned) if planned > 0 else 0.0
    if presence_source == "events":
        # 没有心跳的旧客户端只能证明「会话跨了这么久」，不能证明人在屏幕前：
        # 时间分最多给一半，并打 no_heartbeat，避免纯挂机刷出「课时完成」。
        time_ratio = min(time_ratio, 0.5)
    if not course_started_at:
        # 没有 course_start：可能停在开课闸门/只是打开页面，不能按整段 wall time 算课时。
        time_ratio = min(time_ratio, 0.5)
    completion = _clip01(0.45 * time_ratio + 0.35 * phase_ratio + 0.20 * quiz_ratio)

    turns = _student_turns(lesson_events)
    prompts = _prompt_count(lesson_events)
    response_rate = _clip01(quiz["answered_prompts"] / prompts) if prompts else 0.0
    per_10min = turns["questions"] / max(active / 600.0, 0.1)
    proactive_score = _clip01(per_10min / 2.0)
    meaningful_rate = _clip01((turns["answers"] + turns["questions"]) / max(prompts, 1))
    interaction = _clip01(
        0.50 * response_rate + 0.25 * proactive_score + 0.25 * meaningful_rate
    )

    mastery = quiz["accuracy"]
    flags: list[str] = []
    if quiz["quiz_issued"] < 1:
        flags.append("no_quiz")
    if quiz["attempts"] < 1:
        flags.append("no_answer")
    if interaction < INTERACTION_LOW:
        flags.append("low_interaction")
    if mastery is not None and mastery < MASTERY_LOW:
        flags.append("low_mastery")
    if not summary_reached:
        flags.append("no_summary")
    if time_ratio < 0.5:
        flags.append("left_early")
    if presence_source == "events":
        flags.append("no_heartbeat")
    if not course_started_at:
        flags.append("no_course_start")
    safety_alert = any(item.get("event_type") == "crisis_alert" for item in events)
    if safety_alert:
        flags.append("safety_alert")

    if not ended and (time.time() - ended_at) < SESSION_ACTIVE_WINDOW:
        status = "in_progress"
    elif not ended:
        # 没有 session_end 且早已无事件：多半是断线/关页后没走收尾，按中途离开处理
        status = "abandoned"
    elif time_ratio < 0.50:
        status = "abandoned"
    elif quiz["first_attempts"] < 1 and time_ratio >= 0.80 and phase_ratio >= 0.75:
        # 课上完了但没有出题/作答证据：不能算“完成学习”，也不该误报成“没上完”
        status = "unverified"
    elif safety_alert:
        # 出现危机/安全事件优先进入人工复核，不自动判定“完成学习”
        status = "partial"
    elif completion >= COMPLETION_PASS:
        if quiz["first_attempts"] < 1:
            status = "unverified"
        elif mastery is not None and mastery < MASTERY_LOW:
            status = "attended_not_learned"
        elif (
            mastery is None
            or mastery < MASTERY_PASS
            or quiz["first_attempts"] < MIN_FIRST_ATTEMPTS
            or not summary_reached
        ):
            status = "partial"
        else:
            status = "completed"
    else:
        status = "partial"

    first_event = _payload(events[0])
    return {
        "schema_version": REPORT_VERSION,
        "session_id": sid,
        "student_id": str(events[0].get("student_id") or ""),
        "subject": str(
            first_event.get("subject")
            or next((item.get("subject") for item in events if item.get("subject")), "")
            or ""
        ),
        "stage": str(events[0].get("stage") or start_payload.get("stage") or ""),
        "grade": str(start_payload.get("grade") or ""),
        "teacher": str(start_payload.get("teacher") or ""),
        "started_at": started_at,
        "session_started_at": session_started_at,
        "ended_at": ended_at,
        "ended": ended,
        "planned_seconds": int(round(planned)),
        "active_seconds": int(round(active)),
        "presence_source": presence_source,
        "phases": phases,
        "summary_reached": summary_reached,
        "time_ratio": round(time_ratio, 3),
        "phase_ratio": round(phase_ratio, 3),
        "quiz_issued": quiz["quiz_issued"],
        "quiz_answered": quiz["quiz_answered"],
        "quiz_ratio": round(quiz_ratio, 3),
        "completion_rate": round(completion, 3),
        "attempts": quiz["attempts"],
        "first_attempts": quiz["first_attempts"],
        "correct_first": quiz["correct_first"],
        "accuracy": None if mastery is None else round(mastery, 3),
        "mastery": None if mastery is None else round(mastery, 3),
        "student_turns": turns["turns"],
        "answers": turns["answers"],
        "questions": turns["questions"],
        "images": turns["images"],
        "prompts": prompts,
        "answered_prompts": quiz["answered_prompts"],
        "response_rate": round(response_rate, 3),
        "proactive_per_10min": round(per_10min, 3),
        "meaningful_rate": round(meaningful_rate, 3),
        "interaction_rate": round(interaction, 3),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "flags": flags,
        "event_count": len(events),
        "updated_at": time.time(),
    }


def summarize_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """多节课概览（看板顶部卡片）。"""
    total = len(reports)
    completed = sum(1 for item in reports if item.get("status") == "completed")
    attended = sum(1 for item in reports if item.get("status") == "attended_not_learned")
    unverified = sum(1 for item in reports if item.get("status") == "unverified")
    partial = sum(1 for item in reports if item.get("status") == "partial")
    abandoned = sum(1 for item in reports if item.get("status") == "abandoned")
    in_progress = sum(1 for item in reports if item.get("status") == "in_progress")

    def avg(key: str) -> float | None:
        values = [
            float(item[key])
            for item in reports
            if isinstance(item.get(key), (int, float))
        ]
        return round(sum(values) / len(values), 3) if values else None

    return {
        "sessions": total,
        "completed": completed,
        "attended_not_learned": attended,
        "unverified": unverified,
        "partial": partial,
        "abandoned": abandoned,
        "in_progress": in_progress,
        "completion_rate": avg("completion_rate"),
        "mastery": avg("mastery"),
        "interaction_rate": avg("interaction_rate"),
        "active_seconds": sum(int(item.get("active_seconds") or 0) for item in reports),
    }
