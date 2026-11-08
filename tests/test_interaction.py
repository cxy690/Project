"""互动追问闭环测试：讲完发起追问 -> 学生作答 -> 判定/反馈/重试。"""
from __future__ import annotations

import json

import pytest

from agents.teacher.agent import TeacherAgent
from config.settings import settings
from core.langgraph_flow import LANGGRAPH_AVAILABLE
from core.state.models import StudentTurn
from core.state.session import SessionState
from core.supervisor import Supervisor
from models.base import BaseLLMClient
from models.tts import MockTTS


@pytest.fixture(autouse=True)
def _force_interaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """这些用例验证互动追问本身，强制开启（与 .env 隔离）。"""
    monkeypatch.setattr(settings, "interaction_enable", True)


FOLLOWUP = {
    "question": "2加3等于几？",
    "answer": "5",
    "hint": "掰手指算算",
    "options": ["4", "5", "6"],
}


class FakeLLM(BaseLLMClient):
    """答疑返回带 followup 的 JSON；判题返回可控的正确性。"""

    is_mock = True

    def __init__(self, followup: dict | None = FOLLOWUP, judge_correct: bool = True) -> None:
        self.followup = followup
        self.judge_correct = judge_correct

    async def chat(self, messages, *, temperature: float = 0.3, max_tokens: int = 1024) -> str:
        joined = " ".join(str(m.get("content")) for m in messages)
        if "批改老师" in joined:
            return json.dumps(
                {"correct": self.judge_correct, "feedback": "很棒！", "hint": "再检查一下"}
            )
        return json.dumps(
            {
                "speech": "我们来看这道题。",
                "title": "测试题",
                "steps": ["读题", "求解"],
                "knowledge_points": [],
                "pitfalls": [],
                "equation": "",
                "answer": "",
                "followup": self.followup,
            }
        )


def _make(
    followup: dict | None = FOLLOWUP,
    judge_correct: bool = True,
) -> tuple[SessionState, Supervisor, list]:
    events: list = []
    state = SessionState(session_id="i", student_id="s")
    state.emit = lambda payload: events.append(payload)
    supervisor = Supervisor(state, teacher=TeacherAgent(llm=FakeLLM(followup, judge_correct)))
    supervisor.tts = MockTTS()  # 避免测试触发真实 TTS 网络请求
    return state, supervisor, events


def _event(events: list, etype: str) -> dict | None:
    return next((e for e in events if e.get("type") == etype), None)


async def test_teach_asks_followup() -> None:
    state, supervisor, events = _make()
    await supervisor.handle_turn(StudentTurn(text="2+3等于几？"))
    assert state.pending_question is not None
    assert state.pending_question["question"] == FOLLOWUP["question"]
    ask = _event(events, "ai_ask")
    assert ask is not None
    assert ask["payload"]["options"] == ["4", "5", "6"]


async def test_spoken_text_includes_question() -> None:
    _, supervisor, _ = _make()
    resp = await supervisor.handle_turn(StudentTurn(text="2+3等于几？"))
    assert resp is not None
    # 播报文本会把追问也读出来（句末标点会被清洗掉）
    assert "2加3等于几" in Supervisor._spoken_text(resp)


async def test_correct_answer_clears_pending() -> None:
    state, supervisor, events = _make(judge_correct=True)
    await supervisor.handle_turn(StudentTurn(text="2+3等于几？"))
    await supervisor.handle_turn(StudentTurn(text="5"))
    check = _event(events, "ai_check")
    assert check is not None and check["payload"]["correct"] is True
    assert state.pending_question is None


async def test_wrong_answer_retry_then_reveal() -> None:
    state, supervisor, events = _make(judge_correct=False)
    await supervisor.handle_turn(StudentTurn(text="2+3等于几？"))
    # 第一次答错：允许重试，pending 保留
    await supervisor.handle_turn(StudentTurn(text="4"))
    first = [e for e in events if e.get("type") == "ai_check"][-1]
    assert first["payload"]["correct"] is False
    assert first["payload"]["allow_retry"] is True
    assert state.pending_question is not None
    # 达到重试上限：公布答案并清空
    await supervisor.handle_turn(StudentTurn(text="4"))
    last = [e for e in events if e.get("type") == "ai_check"][-1]
    assert last["payload"]["allow_retry"] is False
    assert state.pending_question is None


async def test_new_question_not_treated_as_answer() -> None:
    state, supervisor, events = _make()
    await supervisor.handle_turn(StudentTurn(text="2+3等于几？"))
    assert state.pending_question is not None
    # 含疑问特征：视为新答疑而非作答，不应产生 ai_check
    await supervisor.handle_turn(StudentTurn(text="为什么天空是蓝色的？"))
    assert not any(e.get("type") == "ai_check" for e in events)


async def test_langgraph_ask_and_check(monkeypatch: pytest.MonkeyPatch) -> None:
    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")
    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    state, supervisor, events = _make()
    await supervisor.handle_turn(StudentTurn(text="2+3等于几？"))
    assert state.pending_question is not None
    await supervisor.handle_turn(StudentTurn(text="5"))
    assert state.pending_question is None
    check = _event(events, "ai_check")
    assert check is not None and check["payload"]["correct"] is True


async def test_idle_greeting_speaks() -> None:
    """长时间空闲时，AI 应主动问候学生（产生话术）。"""
    _, supervisor, events = _make()
    resp = await supervisor.on_idle()
    assert resp is not None and resp.speech
    assert any(e.get("type") == "ai_speech" for e in events)
    # 学生一有输入，空闲计数归零
    await supervisor.handle_turn(StudentTurn(text="你好"))
    assert supervisor._idle_nudges == 0
