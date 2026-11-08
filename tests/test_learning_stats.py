"""学情分析专项测试：指标口径、采集器、存储、报告。

这些测试钉住的是**口径**，不是实现细节：
* 40 分钟课，学生中途离开 20 分钟 —— 有效学习时长不能算成 40 分钟；
* 老师提问 6 次、学生答 3 次 —— 互动率必须是 0.5；
* 首答全对、无出题覆盖 —— 掌握度不能给满分（还要看练习量）；
* 2 秒内答对难题 —— 真实性要扣分并出提示。
"""
from __future__ import annotations

import time

import pytest

from core.learning_stats import collector as collector_mod
from core.learning_stats import store as store_mod
from core.learning_stats.metrics import (
    IDLE_GAP_SEC,
    compute_metrics,
    effective_time,
    mastery_level_of,
    summarize_lessons,
)
from core.learning_stats.models import EventKind, LessonEvent, LessonMetrics
from core.learning_stats.report import build_report

T0 = 1_700_000_000.0


def _ev(offset: float, kind: EventKind, **data) -> LessonEvent:
    return LessonEvent(ts=T0 + offset, kind=kind.value, data=data)


def _full_lesson() -> list[LessonEvent]:
    """一节"正常"的课：40 分钟、两个环节、6 问 6 答、3 题全对、结尾检测。"""
    events = [_ev(0, EventKind.SESSION_START)]
    events.append(_ev(60, EventKind.PHASE, phase="lecture"))
    for i in range(6):
        events.append(_ev(120 + i * 300, EventKind.TEACHER_QUESTION))
        events.append(_ev(135 + i * 300, EventKind.STUDENT_TEXT, latency=15, initiative=i % 3 == 0))
    events.append(_ev(1900, EventKind.PHASE, phase="interaction"))
    for i in range(3):
        events.append(_ev(2000 + i * 120, EventKind.QUIZ_ASKED, question_id=f"q{i}"))
        events.append(_ev(2030 + i * 120, EventKind.QUIZ_ANSWERED,
                         question_id=f"q{i}", correct=True, latency=30))
    events.append(_ev(2399, EventKind.SESSION_END))
    return events


# ---------------------------------------------------------------- 有效时长
def test_effective_time_excludes_idle_gap() -> None:
    events = [_ev(0, EventKind.SESSION_START), _ev(60, EventKind.STUDENT_TEXT)]
    # 前 60 秒还在场，随后空了 20 分钟（> IDLE_GAP_SEC）才算离场
    events.append(_ev(60 + 20 * 60, EventKind.STUDENT_TEXT))
    effective, idle, count = effective_time(events)
    assert idle >= 20 * 60
    assert count == 1
    assert effective == pytest.approx(60, abs=1)


def test_effective_time_short_session() -> None:
    assert effective_time([]) == (0.0, 0.0, 0)
    assert effective_time([_ev(0, EventKind.SESSION_START)]) == (0.0, 0.0, 0)


# ---------------------------------------------------------------- 完整课
def test_full_lesson_metrics_are_high() -> None:
    metrics = compute_metrics(
        lesson_id="L-full", student_id="s1", subject="数学", grade="初二",
        events=_full_lesson(), planned_seconds=2400,
    )
    assert metrics.completion_score >= 85
    assert metrics.interaction_rate == 1.0
    assert metrics.accuracy == 1.0
    assert metrics.first_accuracy == 1.0
    assert metrics.mastery_score >= 90
    assert metrics.mastery_level in {"应用", "熟练"}
    assert metrics.authenticity_score >= 90
    assert metrics.overall_score >= 85
    assert metrics.stage == "junior"
    assert metrics.wrap_done is True


def test_short_idle_lesson_flags_and_lower_completion() -> None:
    """中途离场 20 分钟：完课度下降 + 真实性扣分 + 有提示。"""
    events = [
        _ev(0, EventKind.SESSION_START),
        _ev(60, EventKind.TEACHER_QUESTION),
        _ev(75, EventKind.STUDENT_TEXT, latency=15),
        _ev(75 + 20 * 60, EventKind.STUDENT_TEXT, latency=10),   # 离场 20 分钟后回来
        _ev(1300, EventKind.SESSION_END),
    ]
    metrics = compute_metrics(
        lesson_id="L-idle", student_id="s1", events=events, planned_seconds=2400
    )
    assert metrics.idle_count == 1
    assert metrics.idle_seconds >= 1200
    assert metrics.completion_score < 60
    assert metrics.authenticity_score < 90
    assert any("离场" in flag for flag in metrics.flags)


# ---------------------------------------------------------------- 互动率
def test_interaction_rate_formula() -> None:
    events = [_ev(0, EventKind.SESSION_START)]
    events += [_ev(100 + i * 60, EventKind.TEACHER_QUESTION) for i in range(6)]
    events += [_ev(120 + i * 60, EventKind.STUDENT_TEXT) for i in range(3)]
    metrics = compute_metrics(
        lesson_id="L-inter", student_id="s", events=events, planned_seconds=600
    )
    assert metrics.teacher_questions == 6
    assert metrics.student_turns == 3
    assert metrics.interaction_rate == 0.5


def test_interaction_rate_capped_at_one() -> None:
    events = [_ev(0, EventKind.SESSION_START)]
    events.append(_ev(30, EventKind.TEACHER_QUESTION))
    events += [_ev(60 + i * 10, EventKind.STUDENT_TEXT) for i in range(5)]
    metrics = compute_metrics(lesson_id="L-cap", student_id="s", events=events)
    assert metrics.interaction_rate == 1.0


def test_initiative_rate() -> None:
    events = [
        _ev(0, EventKind.SESSION_START),
        _ev(10, EventKind.STUDENT_TEXT, initiative=True),
        _ev(20, EventKind.STUDENT_TEXT, initiative=False),
        _ev(30, EventKind.STUDENT_TEXT, initiative=True),
    ]
    metrics = compute_metrics(lesson_id="L-init", student_id="s", events=events)
    assert metrics.student_initiations == 2
    assert metrics.initiative_rate == pytest.approx(2 / 3, abs=1e-3)


# ---------------------------------------------------------------- 掌握度
@pytest.mark.parametrize(
    ("score", "level"),
    [
        (96, "熟练"), (90, "应用"), (80, "理解"), (65, "初识"), (30, "未达标"),
    ],
)
def test_mastery_levels(score: float, level: str) -> None:
    assert mastery_level_of(score) == level


def test_no_quiz_means_low_mastery() -> None:
    events = [_ev(0, EventKind.SESSION_START), _ev(600, EventKind.SESSION_END)]
    metrics = compute_metrics(lesson_id="L-noq", student_id="s", events=events, planned_seconds=600)
    assert metrics.quiz_total == 0
    assert metrics.mastery_score == 0
    assert metrics.mastery_level == "未达标"


def test_first_attempt_flag_counts_once() -> None:
    """同一题答两次（先错后对）：首答不计正确，总数按题算。"""
    events = [
        _ev(0, EventKind.SESSION_START),
        _ev(60, EventKind.QUIZ_ASKED, question_id="q1"),
        _ev(90, EventKind.QUIZ_ANSWERED, question_id="q1", correct=False, latency=30),
        _ev(300, EventKind.PHASE, phase="interaction"),
        _ev(400, EventKind.QUIZ_ANSWERED, question_id="q1", correct=True, latency=20),
    ]
    metrics = compute_metrics(lesson_id="L-retry", student_id="s", events=events, planned_seconds=600)
    assert metrics.quiz_total == 1
    assert metrics.quiz_first_correct == 0
    assert metrics.quiz_correct == 1
    assert metrics.first_accuracy == 0.0
    assert metrics.accuracy == 0.5
    assert 0.5 <= metrics.mastery_score < 70


# ---------------------------------------------------------------- 真实性
def test_fast_correct_answers_trigger_flag() -> None:
    events = [_ev(0, EventKind.SESSION_START)]
    for i in range(3):
        events.append(_ev(100 + i * 60, EventKind.QUIZ_ASKED, question_id=f"q{i}"))
        events.append(_ev(101 + i * 60, EventKind.QUIZ_ANSWERED,
                          question_id=f"q{i}", correct=True, latency=1.0))
    metrics = compute_metrics(lesson_id="L-fast", student_id="s", events=events, planned_seconds=600)
    assert metrics.authenticity_score < 90
    assert any("2 秒" in flag for flag in metrics.flags)


def test_no_student_turns_flag() -> None:
    events = [
        _ev(0, EventKind.SESSION_START),
        _ev(1200, EventKind.TEACHER_QUESTION),
        _ev(1300, EventKind.SESSION_END),
    ]
    metrics = compute_metrics(lesson_id="L-silent", student_id="s", events=events, planned_seconds=2400)
    assert any("几乎没有发言" in flag for flag in metrics.flags)
    assert metrics.authenticity_score < 100


# ---------------------------------------------------------------- 报告
def test_report_has_actionable_sections() -> None:
    metrics = compute_metrics(
        lesson_id="L-report", student_id="s1", subject="语文", grade="五年级",
        events=_full_lesson(), planned_seconds=2400,
    )
    report = build_report(metrics)
    assert report.headline
    assert report.parent_note
    assert report.next_actions
    assert "五年级" in report.metrics.grade or report.metrics.stage == "primary"


def test_report_warns_when_authenticity_low() -> None:
    metrics = LessonMetrics(
        lesson_id="L", student_id="s", overall_score=80,
        authenticity_score=40, mastery_level="理解",
    )
    report = build_report(metrics)
    assert "确认" in report.headline


def test_summarize_lessons() -> None:
    items = [
        LessonMetrics(lesson_id="a", student_id="s", completion_score=80, mastery_score=70,
                      interaction_rate=0.5, authenticity_score=100, overall_score=75,
                      effective_seconds=1200),
        LessonMetrics(lesson_id="b", student_id="s", completion_score=60, mastery_score=50,
                      interaction_rate=0.3, authenticity_score=80, overall_score=55,
                      effective_seconds=600),
    ]
    summary = summarize_lessons(items)
    assert summary["lessons"] == 2
    assert summary["completion"] == 70.0
    assert summary["total_minutes"] == 30.0


# ---------------------------------------------------------------- 存储与采集
@pytest.fixture()
def temp_dirs(tmp_path, monkeypatch):
    """把学情落盘目录指到临时目录，避免污染 data/learning。"""
    lesson_dir = tmp_path / "lessons"
    student_dir = tmp_path / "students"
    monkeypatch.setattr(store_mod, "LESSON_DIR", lesson_dir)
    monkeypatch.setattr(store_mod, "STUDENT_DIR", student_dir)
    monkeypatch.setattr(collector_mod.store, "LESSON_DIR", lesson_dir)
    monkeypatch.setattr(collector_mod.store, "STUDENT_DIR", student_dir)
    collector_mod.get_collector().reset()
    return lesson_dir, student_dir


class _State:
    """最小会话桩：采集器只需要这几个属性。"""

    def __init__(self, session_id: str = "sess-1", student_id: str = "stu-1") -> None:
        self.session_id = session_id
        self.student_id = student_id
        self.subject = "数学"
        self.scratch = {"grade": "六年级"}
        self.events: list[dict] = []

    def emit(self, payload: dict) -> None:
        self.events.append(payload)


def test_store_roundtrip_and_student_index(temp_dirs) -> None:
    lesson_dir, student_dir = temp_dirs
    metrics = compute_metrics(
        lesson_id="L-store", student_id="stu-9", subject="数学",
        events=_full_lesson(), planned_seconds=2400,
    )
    store_mod.write_meta("L-store", {"lesson_id": "L-store", "student_id": "stu-9"})
    store_mod.append_event("L-store", _ev(0, EventKind.SESSION_START))
    store_mod.write_metrics(metrics)

    assert store_mod.read_metrics("L-store").student_id == "stu-9"
    assert len(store_mod.read_events("L-store")) == 1
    assert len(store_mod.list_lessons(student_id="stu-9")) == 1
    index = store_mod.read_student_index("stu-9")
    assert index["totals"]["lessons"] == 1
    assert (lesson_dir).is_dir() and (student_dir).is_dir()


def test_collector_collects_and_finishes(temp_dirs) -> None:
    state = _State()
    collector = collector_mod.get_collector()
    collector.start(state)
    collector.student_message(state, "老师，这道题怎么做", "text")
    collector.session_event(state, "ai_ask", {"question": "1+1 等于几"})
    collector.student_message(state, "2", "text")
    collector.session_event(state, "ai_quiz", {"question_id": "q1"})
    collector.session_event(state, "ai_check", {"correct": True})
    metrics = collector.finish(state, reason="test")

    assert metrics is not None
    assert metrics.student_id == "stu-1"
    assert metrics.teacher_questions == 1
    assert metrics.student_turns == 2
    assert metrics.quiz_total == 1
    assert metrics.quiz_correct == 1
    assert metrics.stage == "primary"
    assert store_mod.read_metrics(metrics.lesson_id) is not None


def test_collector_start_is_idempotent(temp_dirs) -> None:
    state = _State(session_id="sess-2")
    collector = collector_mod.get_collector()
    first = collector.start(state)
    second = collector.start(state)
    assert first is not None and first is second


def test_collector_unknown_event_is_ignored(temp_dirs) -> None:
    state = _State(session_id="sess-3")
    collector = collector_mod.get_collector()
    collector.session_event(state, "ai_speech", {"text": "..."})
    assert collector.snapshot(state) is None

def test_learning_routes_scope_by_org(monkeypatch) -> None:
    """多机构隔离：机构教师只能看本机构学生，平台级账号可跨机构。"""
    from server import learning_routes as lr

    monkeypatch.setattr(
        lr, "get_org_id",
        lambda sid: {"a_stu": "schoola", "b_stu": "schoolb"}.get(str(sid), ""),
    )
    teacher_a = {"id": "t1", "role": "teacher", "org_id": "schoola"}
    assert lr._can_view(teacher_a, "a_stu") is True
    assert lr._can_view(teacher_a, "b_stu") is False
    assert lr._can_view(teacher_a, "unknown") is False

    platform = {"id": "p1", "role": "admin", "org_id": ""}
    assert lr._can_view(platform, "b_stu") is True

    student = {"id": "a_stu", "role": "student", "org_id": "schoola"}
    assert lr._can_view(student, "a_stu") is True
    assert lr._can_view(student, "b_stu") is False
