"""教学图规格的校验与降级（见 core/diagram/spec.py）。

这些用例守的是「画图不能拖垮上课」这条线：模型写错、超时、表达式非法，
都只能导致「这一轮没图」，不能抛异常、不能给出前端渲染不了的结构。
"""
from __future__ import annotations

import asyncio

import pytest

from core.diagram.spec import DiagramSpecBuilder, sanitize


class FakeLLM:
    """最小 LLM 桩：返回固定 JSON 或抛错，并记录调用次数（用于验证缓存）。"""

    def __init__(self, payload=None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001 - 跟真实客户端签名一致即可
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def test_flow_spec_is_normalized():
    """正常输出：id 保留、分步保留、canvas/theme 被强制成横屏浅色。"""
    spec = sanitize({
        "type": "flow",
        "canvas": "portrait",
        "theme": "ink",
        "title": "解题流程",
        "nodes": [
            {"id": "read", "text": "读题"},
            {"id": "list", "text": "列条件"},
            {"id": "solve", "text": "求解"},
        ],
        "edges": [{"from": "read", "to": "list"}, {"from": "list", "to": "solve"}],
        "steps": [
            {"reveal": ["read"], "narration": "先读题"},
            {"reveal": ["list"], "narration": "再列条件"},
            {"reveal": ["solve"], "narration": "最后求解"},
        ],
    })
    assert spec is not None
    assert spec["canvas"] == "landscape" and spec["theme"] == "light"
    assert [n["id"] for n in spec["nodes"]] == ["read", "list", "solve"]
    assert [len(s["reveal"]) for s in spec["steps"]] == [1, 1, 1]


def test_unrevealed_items_are_appended_to_last_step():
    """没被任何一步揭示的图元，在演示里永远不出现 —— 必须补到最后一步。"""
    spec = sanitize({
        "type": "flow",
        "nodes": [
            {"id": "a", "text": "甲"}, {"id": "b", "text": "乙"}, {"id": "c", "text": "丙"},
        ],
        "steps": [{"reveal": ["a"]}, {"reveal": ["b"]}],
    })
    assert spec is not None
    assert "c" in spec["steps"][-1]["reveal"]


def test_unknown_reveal_ids_are_dropped():
    spec = sanitize({
        "type": "flow",
        "nodes": [{"id": "a", "text": "甲"}, {"id": "b", "text": "乙"}],
        "steps": [{"reveal": ["a", "幽灵"]}],
    })
    assert spec is not None
    assert "幽灵" not in str(spec.get("steps"))


def test_curve_drops_bad_expression_keeps_good_one():
    """表达式非法只丢这条曲线，不让整张图陪葬。"""
    spec = sanitize({
        "type": "curve",
        "axes": {"x": {"min": -3, "max": 3}, "y": {"min": -5, "max": 9}},
        "curves": [
            {"id": "par", "label": "y = x² - 2x - 3", "fn": "x^2-2x-3"},
            {"id": "bad", "label": "乱写", "fn": "while(1){}"},
        ],
    })
    assert spec is not None
    assert [c["id"] for c in spec["curves"]] == ["par"]


def test_board_coordinates_are_clamped():
    spec = sanitize({
        "type": "board",
        "strokes": [{"id": "leg", "points": [[-500, 520], [2400, 2000]]}],
        "notes": [{"id": "t", "x": 4000, "y": -20, "text": "A"}],
    })
    assert spec is not None
    assert spec["strokes"][0]["points"] == [[0, 520], [1920, 1080]]
    assert spec["notes"][0]["x"] == 1920 and spec["notes"][0]["y"] == 0


def test_missing_ids_get_placeholders():
    spec = sanitize({
        "type": "layers",
        "layers": [{"label": "地壳", "detail": "约 17 km"}, {"id": "核心", "label": "地核"}],
        "steps": [{"reveal": ["l1"]}, {"reveal": ["l2"]}],
    })
    assert spec is not None
    ids = [layer["id"] for layer in spec["layers"]]
    assert ids == ["l1", "l2"]          # 缺失的中文 id 被换成确定性的占位 id


@pytest.mark.parametrize("raw", [
    None, "x^2", {"type": "hologram"}, {"type": "flow", "nodes": []},
    {"type": "board"},
])
def test_unusable_input_returns_none(raw):
    assert sanitize(raw) is None


def test_builder_returns_none_when_llm_fails(tmp_path):
    """模型报错 = 本轮没图，绝不向上抛（否则整段讲解会被异常打断）。"""
    builder = DiagramSpecBuilder(FakeLLM(error=RuntimeError("boom")), cache_dir=tmp_path)
    assert asyncio.run(builder.build("求抛物线 y=x^2-2x-3 的顶点")) is None


def test_builder_times_out_without_blocking(tmp_path):
    class SlowLLM(FakeLLM):
        async def chat_json(self, messages, **kwargs):  # noqa: ANN001
            await asyncio.sleep(5)
            return {}

    builder = DiagramSpecBuilder(SlowLLM(), timeout=0.05, cache_dir=tmp_path)
    assert asyncio.run(builder.build("求抛物线 y=x^2-2x-3 的顶点")) is None


def test_builder_reuses_cache(tmp_path):
    llm = FakeLLM({
        "type": "flow",
        "nodes": [{"id": "a", "text": "读题"}, {"id": "b", "text": "求解"}],
        "edges": [{"from": "a", "to": "b"}],
        "steps": [{"reveal": ["a"]}, {"reveal": ["b"]}],
    })
    builder = DiagramSpecBuilder(llm, cache_dir=tmp_path)
    first = asyncio.run(builder.build("一道应用题"))
    second = asyncio.run(builder.build("一道应用题"))
    assert first is not None and second is not None
    assert llm.calls == 1               # 同题不重复调模型
    assert first == second


def test_builder_skips_empty_question(tmp_path):
    llm = FakeLLM({})
    builder = DiagramSpecBuilder(llm, cache_dir=tmp_path)
    assert asyncio.run(builder.build("   ")) is None
    assert llm.calls == 0


# ==================================================================== supervisor 接线

def _supervisor(subject: str):
    from agents.supervisor import Supervisor
    from core.state.session import SessionState

    state = SessionState(session_id="diagram-1", student_id="s-diagram")
    state.subject = subject
    events: list[dict] = []
    state.emit = events.append
    return Supervisor(state), state, events


async def test_schedule_diagram_pushes_empty_for_other_subject():
    """非白名单学科：不调模型，但要明确告诉前端「本轮没有图」，清掉上一题。"""
    supervisor, _state, events = _supervisor("英语")
    supervisor._schedule_diagram("求抛物线 y=x^2-2x-3 的顶点")
    assert [e["type"] for e in events] == ["diagram"]
    assert events[0]["payload"]["spec"] is None


async def test_schedule_diagram_returns_before_spec_is_ready(monkeypatch):
    """画图必须后台跑：调度函数返回时规格还没生成，课堂一秒都不等它。"""
    import agents.supervisor.agent as agent_module

    started = asyncio.Event()

    class SlowBuilder:
        async def build(self, question, *, subject=""):  # noqa: ANN001 - 与真实签名一致
            started.set()
            await asyncio.sleep(0.05)
            return {"type": "curve", "canvas": "landscape", "theme": "light",
                    "title": "抛物线", "curves": [{"id": "p", "fn": "x^2-2x-3"}],
                    "steps": [{"reveal": ["p"]}]}

    monkeypatch.setattr(agent_module, "get_diagram_builder", lambda: SlowBuilder())
    supervisor, _state, events = _supervisor("数学")
    supervisor._schedule_diagram("求抛物线 y=x^2-2x-3 的顶点")

    assert events == []                     # 同步返回：没有 await 画图
    await supervisor._diagram_task          # 后台任务收尾后才下发
    assert events[-1]["payload"]["spec"]["type"] == "curve"
