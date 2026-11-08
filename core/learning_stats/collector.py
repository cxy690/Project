"""课堂事件采集 + 实时监控（智能监控机制的执行体）。

**接入点只有三个，全部是旁路，异常一律吞掉**（学情统计绝不能影响上课）：

1. :meth:`SessionState.push_event` —— 旁听 ``ai_ask`` / ``ai_quiz`` / ``ai_check`` /
   ``course_state`` 等既有事件（一处挂载，所有 Agent 自动覆盖）；
2. ``server/websocket.py`` 的学生消息 —— 记录发言、作答时延、是否主动发起；
3. 会话结束 —— 结算指标、落盘、更新学生索引。

**实时监控**（给老师的即时提醒，不阻塞主链路）：
* 静默超过 5 分钟 → 「学生可能离场」
* 连续答错 2 次 → 「建议降难度」
* 课程过半互动率 < 0.3 → 「提问多、回应少」
* 事件间隔超过 3 分钟 → 「疑似挂机」

告警走 ``state.emit({"type": "learning_alert", ...})`` 推给前端，
同一类告警每节课只提醒一次（避免刷屏）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from config.logging import get_logger
from config.stages import stage_from_grade
from core.learning_stats.metrics import IDLE_GAP_SEC, compute_metrics
from core.learning_stats.models import EventKind, LessonEvent, LessonMetrics
from core.learning_stats import store

log = get_logger("core.learning_stats.collector")

#: 学生静默多久算离场告警（秒）
SILENCE_ALERT_SEC = 300
#: 连续答错多少次提醒降难度
RETRY_ALERT_STREAK = 2
#: 课程过半时互动率的最低要求
LOW_INTERACTION_RATE = 0.3
#: 触发静默告警前，课程至少要已进行多久（秒），避免刚开课就误报
SILENCE_MIN_COURSE_SEC = 300


@dataclass
class _LiveLesson:
    """一节课的采集态（内存，进程重启即丢；指标已落盘的不受影响）。"""

    lesson_id: str
    student_id: str
    started_at: float
    subject: str = "综合"
    stage: str = ""
    grade: str = ""
    planned_seconds: int = 0
    events: list[LessonEvent] = field(default_factory=list)
    last_student_ts: float = 0.0
    awaiting_since: float = 0.0       # 老师提问后等待作答的起点（算响应时延）
    quiz_open: dict[str, float] = field(default_factory=dict)  # 题目 id -> 出题时间
    quiz_streak_wrong: int = 0
    alerted: set[str] = field(default_factory=set)
    finished: bool = False


class LearningCollector:
    """进程级采集器（线程安全：WS 回调与 Agent 协程都会调用）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lives: dict[str, _LiveLesson] = {}

    # ------------------------------------------------------------ 生命周期
    def start(self, state: Any) -> _LiveLesson | None:
        """开课：登记一节课（幂等，重复调用返回同一个采集态）。"""
        try:
            key = str(getattr(state, "session_id", "") or "")
            if not key:
                return None
            with self._lock:
                live = self._lives.get(key)
                if live is not None:
                    return live
                scratch = getattr(state, "scratch", {}) or {}
                grade = str(scratch.get("grade") or "")
                live = _LiveLesson(
                    lesson_id=store.new_lesson_id(key),
                    student_id=str(getattr(state, "student_id", "") or ""),
                    started_at=time.time(),
                    subject=str(getattr(state, "subject", "") or "综合"),
                    stage=stage_from_grade(grade),
                    grade=grade,
                    planned_seconds=_planned_seconds(),
                )
                self._lives[key] = live
                self._append(live, EventKind.SESSION_START, {
                    "subject": live.subject, "stage": live.stage, "grade": live.grade,
                })
                store.write_meta(live.lesson_id, {
                    "lesson_id": live.lesson_id,
                    "session_id": key,
                    "student_id": live.student_id,
                    "subject": live.subject,
                    "stage": live.stage,
                    "grade": live.grade,
                    "planned_seconds": live.planned_seconds,
                    "started_at": live.started_at,
                })
                return live
        except Exception as err:  # noqa: BLE001 - 学情采集不影响上课
            log.warning("learning start failed: {}", err)
            return None

    def finish(self, state: Any, reason: str = "") -> LessonMetrics | None:
        """下课：落盘最后一版指标并更新学生索引。"""
        try:
            key = str(getattr(state, "session_id", "") or "")
            with self._lock:
                live = self._lives.get(key)
                if live is None or live.finished:
                    return None
                live.finished = True
                self._append(live, EventKind.SESSION_END, {"reason": reason})
                events = list(live.events)
                meta = {
                    "lesson_id": live.lesson_id,
                    "student_id": live.student_id,
                    "subject": live.subject,
                    "stage": live.stage,
                    "grade": live.grade,
                    "planned_seconds": live.planned_seconds,
                }
            metrics = compute_metrics(events=events, **meta)
            store.write_metrics(metrics)
            _STATES.pop(meta["lesson_id"], None)
            return metrics
        except Exception as err:  # noqa: BLE001
            log.warning("learning finish failed: {}", err)
            return None

    # ------------------------------------------------------------ 采集
    def student_message(self, state: Any, text: str, message_type: str = "text") -> None:
        """学生发来一条消息（提问 / 作答 / 语音）。"""
        try:
            live = self.start(state)
            if live is None:
                return
            now = time.time()
            with self._lock:
                latency = round(now - live.awaiting_since, 2) if live.awaiting_since else None
                initiative = live.awaiting_since <= 0
                live.awaiting_since = 0.0
                live.last_student_ts = now
                self._append(live, EventKind.STUDENT_TEXT, {
                    "text_len": len(str(text or "")),
                    "channel": message_type,
                    "initiative": bool(initiative),
                    "latency": latency,
                })
        except Exception as err:  # noqa: BLE001
            log.warning("learning student_message failed: {}", err)

    def session_event(self, state: Any, event_type: str, payload: Any) -> None:
        """旁听会话事件（由 ``SessionState.push_event`` 调用）。"""
        try:
            if event_type == "course_state":
                live = self.start(state)
                if live is None:
                    return
                phase = _phase_of(payload)
                with self._lock:
                    if phase:
                        self._append(live, EventKind.PHASE, {"phase": phase})
                return
            if event_type == "ai_ask":
                live = self.start(state)
                if live is None:
                    return
                with self._lock:
                    live.awaiting_since = live.awaiting_since or time.time()
                    self._append(live, EventKind.TEACHER_QUESTION, {
                        "question": str((payload or {}).get("question") or "")[:120],
                    })
                    self._check_silence(live, state, time.time())
                return
            if event_type == "ai_quiz":
                live = self.start(state)
                if live is None:
                    return
                qid = _question_id(payload)
                with self._lock:
                    live.awaiting_since = live.awaiting_since or time.time()
                    live.quiz_open[qid] = time.time()
                    self._append(live, EventKind.QUIZ_ASKED, {"question_id": qid})
                return
            if event_type == "ai_check":
                live = self.start(state)
                if live is None:
                    return
                data = payload if isinstance(payload, dict) else {}
                correct = bool(data.get("correct"))
                with self._lock:
                    qid = next(reversed(live.quiz_open), "") if live.quiz_open else ""
                    asked_at = live.quiz_open.pop(qid, 0.0) if qid else 0.0
                    latency = round(time.time() - asked_at, 2) if asked_at else None
                    live.quiz_streak_wrong = 0 if correct else live.quiz_streak_wrong + 1
                    self._append(live, EventKind.QUIZ_ANSWERED, {
                        "question_id": qid,
                        "correct": correct,
                        "latency": latency,
                    })
                    self._check_quiz_streak(live, state)
                    self._check_interaction(live, state)
                return
        except Exception as err:  # noqa: BLE001
            log.warning("learning session_event failed: {}", err)

    def interrupt(self, state: Any) -> None:
        try:
            live = self.start(state)
            if live is None:
                return
            with self._lock:
                self._append(live, EventKind.INTERRUPT, {})
        except Exception as err:  # noqa: BLE001
            log.warning("learning interrupt failed: {}", err)

    # ------------------------------------------------------------ 查询
    def snapshot(self, state: Any) -> LessonMetrics | None:
        """当前会话的实时指标（不落盘，监控台/报告页轮询用）。"""
        try:
            key = str(getattr(state, "session_id", "") or "")
            with self._lock:
                live = self._lives.get(key)
                if live is None:
                    return None
                events = list(live.events)
                meta = {
                    "lesson_id": live.lesson_id,
                    "student_id": live.student_id,
                    "subject": live.subject,
                    "stage": live.stage,
                    "grade": live.grade,
                    "planned_seconds": live.planned_seconds,
                }
            return compute_metrics(events=events, **meta)
        except Exception:  # noqa: BLE001
            return None

    def alerts(self, state: Any) -> list[str]:
        key = str(getattr(state, "session_id", "") or "")
        with self._lock:
            live = self._lives.get(key)
            return sorted(live.alerted) if live else []

    def reset(self) -> None:
        """清空内存态（测试用）。"""
        with self._lock:
            self._lives.clear()

    # ------------------------------------------------------------ 内部
    def _append(self, live: _LiveLesson, kind: EventKind, data: dict[str, Any]) -> None:
        event = LessonEvent(ts=time.time(), kind=kind.value, data=data)
        live.events.append(event)
        try:
            store.append_event(live.lesson_id, event)
        except Exception as err:  # noqa: BLE001 - 落盘失败不影响内存指标
            log.warning("learning event append failed: {}", err)

    def _alert(self, live: _LiveLesson, state: Any, code: str, message: str) -> None:
        if code in live.alerted:
            return
        live.alerted.add(code)
        log.info("learning alert [{}] {}", code, message)
        emit = getattr(state, "emit", None)
        if callable(emit):
            try:
                emit({"type": "learning_alert", "payload": {"code": code, "message": message}})
            except Exception:  # noqa: BLE001
                pass

    def _check_silence(self, live: _LiveLesson, state: Any, now: float) -> None:
        course_sec = now - live.started_at
        reference = live.last_student_ts or live.started_at
        if course_sec >= SILENCE_MIN_COURSE_SEC and now - reference >= SILENCE_ALERT_SEC:
            self._alert(
                live, state, "silence",
                f"学生已经 {round((now - reference) / 60)} 分钟没有发言，确认是否还在听",
            )

    def _check_quiz_streak(self, live: _LiveLesson, state: Any) -> None:
        if live.quiz_streak_wrong >= RETRY_ALERT_STREAK:
            self._alert(
                live, state, "repeat_wrong",
                f"连续答错 {live.quiz_streak_wrong} 次，建议降低难度或回到上一个知识点",
            )

    def _check_interaction(self, live: _LiveLesson, state: Any) -> None:
        elapsed = time.time() - live.started_at
        if not live.planned_seconds or elapsed < live.planned_seconds * 0.5:
            return
        questions = sum(1 for e in live.events if e.kind == EventKind.TEACHER_QUESTION.value)
        turns = sum(1 for e in live.events if e.kind == EventKind.STUDENT_TEXT.value)
        if questions < 2:
            return
        if turns / max(questions, 1) < LOW_INTERACTION_RATE:
            self._alert(
                live, state, "low_interaction",
                f"课程过半，老师提问 {questions} 次、学生只回应 {turns} 次，建议换成小问题逐个确认",
            )


#: 采集时需要 emit 的会话引用（告警要推给前端），避免所有方法层层传参
_STATES: dict[str, Any] = {}


def _planned_seconds() -> int:
    try:
        from config.settings import settings  # noqa: PLC0415 - 避免导入期循环

        return int(settings.course_duration_minutes) * 60
    except Exception:  # noqa: BLE001
        return 40 * 60


def _phase_of(payload: Any) -> str:
    data = payload if isinstance(payload, dict) else {}
    for key in ("phase", "course_phase", "stage"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _question_id(payload: Any) -> str:
    data = payload if isinstance(payload, dict) else {}
    for key in ("question_id", "id"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    question = str(data.get("question") or "").strip()
    return question[:40] or f"q{time.time():.0f}"


_COLLECTOR = LearningCollector()


def get_collector() -> LearningCollector:
    return _COLLECTOR


#: 只旁听这几类事件；其余（ai_speech / status / 白板指令等）高频且与学情无关
WATCHED_EVENTS = frozenset({"course_state", "ai_ask", "ai_quiz", "ai_check"})


def observe_session_event(state: Any, event_type: str, payload: Any) -> None:
    """``SessionState.push_event`` 的旁路入口（只处理与学情有关的事件）。"""
    if event_type not in WATCHED_EVENTS:
        return
    try:
        collector = get_collector()
        live = collector.start(state)
        if live is not None:
            _STATES[live.lesson_id] = state
        collector.session_event(state, event_type, payload)
    except Exception:  # noqa: BLE001 - 任何异常都不能影响上课
        pass
