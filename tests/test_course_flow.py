"""1v1 学科辅导课流程单测（注入 Mock LLM/TTS，无需外部依赖）。"""
from __future__ import annotations

from agents.teacher.agent import TeacherAgent
from config.constants import ClassPhase
from config.subjects import SUBJECTS, resolve_subject
from core.course_session import duration_seconds, phase_at, phase_label, tutorial_plan
from core.state.models import StudentTurn
from core.state.session import SessionState
from core.supervisor import Supervisor
from models.llm import MockLLM
from models.tts import MockTTS


def _build(state: SessionState) -> tuple[Supervisor, list[dict]]:
    """构造 Supervisor：Mock LLM/TTS + 事件收集器（保证单测离线、确定）。"""
    supervisor = Supervisor(state, teacher=TeacherAgent(llm=MockLLM()))
    supervisor.tts = MockTTS()
    events: list[dict] = []
    state.emit = events.append
    return supervisor, events


# ---------------------------------------------------------------- 学科与阶段
def test_subjects_cover_primary_and_junior() -> None:
    names = [s["name"] for s in SUBJECTS]
    for expected in ("语文", "数学", "英语", "物理", "化学", "生物", "地理", "历史"):
        assert expected in names
    # 前端可能传 key 或中文名，需都能归一化
    assert resolve_subject("math") == "数学"
    assert resolve_subject("数学") == "数学"


def test_tutorial_plan_and_phase_mapping() -> None:
    plan = tutorial_plan()
    assert [p["phase"] for p in plan] == [
        ClassPhase.OPENING.value,
        ClassPhase.LECTURE.value,
        ClassPhase.INTERACTION.value,
        ClassPhase.SUMMARY.value,
    ]
    total = duration_seconds()
    assert sum(p["duration_seconds"] for p in plan) == total
    assert phase_at(0) is ClassPhase.OPENING
    assert phase_at(total + 1) is ClassPhase.ENDED
    assert phase_label(ClassPhase.INTERACTION) == "互动出题"


# ---------------------------------------------------------------- 打招呼
async def test_course_start_greets_with_subject_and_wave() -> None:
    state = SessionState(session_id="c1", student_id="s1")
    supervisor, events = _build(state)

    response = await supervisor.on_course_start("数学")

    assert response is not None
    assert "数学" in response.speech                      # 学科出现在招呼语中
    assert response.meta.get("gesture") == "greet"        # 触发挥手动作
    assert response.meta.get("mood") == "greeting"
    assert state.course_active is True
    assert state.course_deadline is not None
    assert any(e["type"] == "course_state" for e in events)


# ---------------------------------------------------------------- 出题 + 判题
async def test_quiz_generation_and_correct_answer_feedback() -> None:
    state = SessionState(session_id="c2", student_id="s2")
    supervisor, events = _build(state)
    await supervisor.on_course_start("语文")

    await supervisor.on_quiz_request("古诗默写")

    assert state.quiz is not None
    assert state.quiz_count == 1
    assert state.pending_question is not None
    quiz_events = [e for e in events if e["type"] == "ai_quiz"]
    assert quiz_events and quiz_events[-1]["payload"]["question"]

    # 按选项/标准答案作答 -> 复用既有追问判题链路
    pending = state.pending_question
    options = list(pending.get("options") or [])
    expected = str(pending.get("expected") or "")
    answer = expected if expected and (not options or expected in options) else (options[0] if options else expected)
    await supervisor.handle_turn(StudentTurn(text=answer))

    checks = [e for e in events if e["type"] == "ai_check"]
    assert checks, "应产出 ai_check 判定事件"
    assert checks[-1]["payload"]["correct"] is True
    assert state.quiz is None          # 答对后收起当前模拟题


async def test_explaining_then_quiz_in_course_mode() -> None:
    """辅导课模式下：学生提问 -> 讲解 -> 自动出模拟题。"""
    state = SessionState(session_id="c3", student_id="s3")
    supervisor, events = _build(state)
    await supervisor.on_course_start("数学")

    await supervisor.handle_turn(StudentTurn(text="老师，一元一次方程怎么解？"))

    assert any(e["type"] == "ai_quiz" for e in events)
    assert state.quiz_count >= 1
    assert state.pending_question is not None


# ---------------------------------------------------------------- 阶段切换
async def test_course_phase_announces_transition() -> None:
    state = SessionState(session_id="c4", student_id="s4")
    supervisor, events = _build(state)
    await supervisor.on_course_start("英语")

    response = await supervisor.on_course_phase("lecture")

    assert response is not None and response.speech
    assert state.course_phase == ClassPhase.LECTURE.value

    # 结束：仅更新状态，不再播报
    await supervisor.on_course_phase("ended")
    assert state.course_active is False
    assert state.course_phase == ClassPhase.ENDED.value
