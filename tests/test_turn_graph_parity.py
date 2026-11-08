"""回合主图一致性测试：builtin（Supervisor 内建）与 langgraph 两条链路行为应一致。"""
from __future__ import annotations

import pytest

from agents.teacher.agent import TeacherAgent
from config.constants import IntentType
from config.settings import settings
from core.langgraph_flow import LANGGRAPH_AVAILABLE
from core.state.models import AIResponse, StudentTurn
from core.state.session import SessionState
from core.supervisor import Supervisor
from models.asr import MockASR
from models.llm import MockLLM
from models.tts import MockTTS


def _new_supervisor() -> Supervisor:
    session = SessionState(session_id="sess", student_id="stu")
    supervisor = Supervisor(session, teacher=TeacherAgent(llm=MockLLM()), retriever=None)
    supervisor.tts = MockTTS()  # 避免测试触发真实 TTS 网络请求
    supervisor.asr = MockASR()
    return supervisor


async def _run(text: str, intent: IntentType | None = None) -> tuple[Supervisor, AIResponse | None]:
    supervisor = _new_supervisor()
    turn = StudentTurn(text=text, intent=intent) if intent is not None else StudentTurn(text=text)
    return supervisor, await supervisor.handle_turn(turn)


async def test_control_turn_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """控制指令：两条链路都返回 None，且产生相同的设备副作用。"""
    monkeypatch.setattr(settings, "orchestrator", "builtin")
    sup_b, resp_b = await _run("关闭麦克风")

    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")
    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    sup_l, resp_l = await _run("关闭麦克风")

    assert resp_b is None and resp_l is None
    assert sup_b.student.microphone_on is False
    assert sup_b.student.microphone_on == sup_l.student.microphone_on


async def test_chitchat_turn_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """闲聊：两条链路都产出同样的温柔回应。"""
    monkeypatch.setattr(settings, "orchestrator", "builtin")
    _, resp_b = await _run("你好")

    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")
    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    _, resp_l = await _run("你好")

    assert resp_b is not None and resp_l is not None
    assert resp_b.speech and resp_b.speech == resp_l.speech


async def test_question_turn_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """答疑：两条链路的 speech 与白板指令序列一致。"""
    monkeypatch.setattr(settings, "orchestrator", "builtin")
    _, resp_b = await _run("为什么 1+1=2？")

    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")
    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    _, resp_l = await _run("为什么 1+1=2？")

    assert resp_b is not None and resp_l is not None
    assert resp_b.speech and resp_l.speech
    assert [op.op for op in resp_b.board_ops] == [op.op for op in resp_l.board_ops]


async def test_langgraph_turn_graph_compiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关指向 langgraph 时，应真正构建并缓存回合主图。"""
    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")
    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    supervisor, _ = await _run("讲讲这道题")
    assert supervisor._turn_graph is not None
