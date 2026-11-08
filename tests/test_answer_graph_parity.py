"""答疑子图一致性测试：builtin（TaskGraph）与 langgraph 两条链路产物应一致。"""
from __future__ import annotations

import pytest

from agents.teacher.agent import TeacherAgent
from config.constants import IntentType
from config.settings import settings
from core import supervisor as supervisor_module
from core.langgraph_flow import LANGGRAPH_AVAILABLE
from core.state.models import AIResponse, StudentTurn
from core.state.session import SessionState
from core.supervisor import Supervisor
from models.llm import MockLLM
from models.tts import MockTTS


async def _answer(text: str) -> AIResponse | None:
    """用离线 MockLLM 跑一次完整答疑回合，返回 AI 产物。"""
    session = SessionState(session_id="sess", student_id="stu")
    supervisor = Supervisor(session, teacher=TeacherAgent(llm=MockLLM()), retriever=None)
    supervisor.tts = MockTTS()  # 避免测试触发真实 TTS 网络请求
    return await supervisor.handle_turn(StudentTurn(intent=IntentType.TEXT_QUESTION, text=text))


async def test_builtin_and_langgraph_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """两条链路应产出结构一致的 AIResponse。"""
    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")

    monkeypatch.setattr(settings, "orchestrator", "builtin")
    builtin = await _answer("为什么 1+1=2？")

    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    langgraph = await _answer("为什么 1+1=2？")

    assert builtin is not None and langgraph is not None
    assert builtin.speech and langgraph.speech
    assert [op.op for op in builtin.board_ops] == [op.op for op in langgraph.board_ops]


async def test_langgraph_path_compiles_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关指向 langgraph 且依赖可用时，应真正构建并缓存 LangGraph 图。"""
    if not LANGGRAPH_AVAILABLE:
        pytest.skip("langgraph not installed")

    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    session = SessionState(session_id="sess", student_id="stu")
    supervisor = Supervisor(session, teacher=TeacherAgent(llm=MockLLM()), retriever=None)
    supervisor.tts = MockTTS()
    await supervisor.handle_turn(StudentTurn(intent=IntentType.TEXT_QUESTION, text="讲讲这道题"))
    assert supervisor._lg_graph is not None


async def test_langgraph_switch_falls_back_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关指向 langgraph 但依赖缺失时，应自动回退自研图并正常产出话术。"""
    monkeypatch.setattr(settings, "orchestrator", "langgraph")
    monkeypatch.setattr(supervisor_module, "LANGGRAPH_AVAILABLE", False)
    response = await _answer("这道题怎么做？")
    assert response is not None and response.speech
