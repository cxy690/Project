"""教学配图（``agents/shared/figure.py``）回归测试。

为什么这些用例重要：配图是**模型输出直接决定画什么**的通道。
采样算错 → 学生看到一条水平线；范围算错 → 顶点跑出画外；
坐标框越界 → 图盖住左侧讲解文字。这几类错误都比「不画图」更糟，
所以每一条都要在这里钉住。
"""
from __future__ import annotations

import json

import pytest

from agents.shared.figure import build_figure
from agents.shared.whiteboard import Whiteboard
from config.constants import BoardOp
from core.state.models import AIResponse
from core.validator import ResponseValidator, drop_invalid_board_ops


# ---------------------------------------------------------------- 函数图像
def test_function_samples_real_curve() -> None:
    """表达式必须真的按 x 求值：定点 (1, -4) 要落在采样里。"""
    fig = build_figure({"type": "function", "expr": "x^2-2*x-3", "x_range": [-2, 4]})
    assert fig is not None
    pts = fig["series"][0]["points"]
    assert len(pts) == 260
    ys = [p[1] for p in pts]
    assert min(ys) == pytest.approx(-4.0, abs=0.05)
    assert fig["y_range"][0] < -4 < fig["y_range"][1]


def test_function_rejects_unsafe_or_empty_expr() -> None:
    """表达式白名单挡住注入；清不出东西就干脆不出图。"""
    assert build_figure({"type": "function", "expr": "__import__('os').system('ls')"}) is None
    assert build_figure({"type": "function", "expr": "求这条直线的解析式"}) is None
    assert build_figure({"type": "function", "expr": ""}) is None
    assert build_figure({"type": "function"}) is None


def test_function_drops_marks_out_of_range() -> None:
    """越界的标注点直接丢，绝不能让前端画到板面之外。"""
    fig = build_figure({
        "type": "function",
        "expr": "x*x",
        "x_range": [-2, 2],
        "marks": [
            {"at": [99, 99], "label": "越界"},
            {"at": [1, 1], "label": "在图上"},
        ],
    })
    assert fig is not None
    assert [m["label"] for m in fig["marks"]] == ["在图上"]


def test_function_accepts_subject_variable_and_math_funcs() -> None:
    """题目变量是 r 也必须能画：自变量统一收敛成 x（线上漏图的主因）。

    学生问「r²+10r+10=100」，模型照着题目把 expr 写成 r —— 旧白名单只认 x，
    整张图被静默丢掉。这里把「变量名不是 x」和「带 sqrt/sin」都钉住。
    """
    fig = build_figure({"type": "function", "expr": "r^2+10*r-90", "x_range": [-15, 5]})
    assert fig is not None
    ys = [p[1] for p in fig["series"][0]["points"]]
    assert min(ys) == pytest.approx(-115.0, abs=2.0), "顶点 r=-5 应落在采样里"

    assert build_figure({"type": "function", "expr": "sqrt(x)", "x_range": [0, 9]}) is not None
    assert build_figure({"type": "function", "expr": "sin(x)", "x_range": [0, 6]}) is not None


def test_function_still_rejects_code_like_input() -> None:
    """字母放行之后，代码味输入仍然只得到「不出图」，绝不会被执行。"""
    assert build_figure({"type": "function", "expr": "import os"}) is None
    assert build_figure({"type": "function", "expr": "os.system"}) is None


# ---------------------------------------------------------------- 数轴
def test_numberline_keeps_ray_and_hollow_point() -> None:
    fig = build_figure({
        "type": "numberline",
        "range": [-1, 5],
        "rays": [[2, 5]],
        "points": [{"at": 2, "hollow": True, "label": "不含 2"}],
        "note": "解集：x > 2",
    })
    assert fig is not None
    kinds = [m["kind"] for m in fig["marks"]]
    assert "ray" in kinds and "point" in kinds and "text" in kinds
    point = next(m for m in fig["marks"] if m["kind"] == "point")
    assert point["hollow"] is True
    assert point["label"] == "不含 2"


# ---------------------------------------------------------------- 平面几何
def test_geometry_builds_edges_and_right_angle() -> None:
    fig = build_figure({
        "type": "geometry",
        "points": [[0, 0], [4, 0], [0, 3]],
        "names": ["A", "B", "C"],
        "side_labels": [{"on": ["A", "B"], "text": "4"}],
        "right_angle": {"at": "A", "arms": ["B", "C"]},
    })
    assert fig is not None
    assert set(fig["vertices"]) == {"A", "B", "C"}
    assert len(fig["edges"]) == 3
    assert fig["right_angle"]["at"] == "A"
    assert fig["side_labels"][0]["text"] == "4"


def test_geometry_needs_three_points() -> None:
    assert build_figure({"type": "geometry", "points": [[0, 0], [1, 1]]}) is None


# ---------------------------------------------------------------- 统计图 / 流程
def test_bar_keeps_categories_and_pads_axis() -> None:
    fig = build_figure({"type": "bar", "categories": ["60以下", "60-69"], "values": [3, 8]})
    assert fig is not None
    assert fig["values"] == [3.0, 8.0]
    assert fig["y_range"][1] > 8, "柱顶要留出写数值的空间"
    assert fig["categories"] == ["60以下", "60-69"]


def test_flow_needs_two_nodes() -> None:
    assert build_figure({"type": "flow", "nodes": ["去分母"]}) is None
    fig = build_figure({"type": "flow", "nodes": ["去分母", "去括号", "移项"]})
    assert fig is not None
    assert fig["nodes"] == ["去分母", "去括号", "移项"]


def test_force_needs_two_arrows() -> None:
    assert build_figure({"type": "force", "arrows": [{"dx": 0, "dy": -2}]}) is None
    fig = build_figure({
        "type": "force",
        "arrows": [
            {"dx": 0, "dy": -2, "label": "G 重力"},
            {"dx": 2, "dy": 0, "label": "F 拉力"},
        ],
    })
    assert fig is not None
    assert len(fig["arrows"]) == 2


# ---------------------------------------------------------------- 统计图表（ECharts）
def test_chart_line_and_bar_normalize_series() -> None:
    line = build_figure({
        "type": "chart", "chart_type": "line",
        "categories": ["一月", "二月", "三月"],
        "series": [{"name": "气温", "values": [3, 8, 14]}],
    })
    assert line is not None
    assert line["chart_type"] == "line"
    assert line["categories"] == ["一月", "二月", "三月"]
    assert line["series"][0]["values"] == [3.0, 8.0, 14.0]
    assert line["y_range"][0] == 0 and line["y_range"][1] > 14, "折线要从 0 起且给顶部留白"

    bar = build_figure({
        "type": "chart", "chart_type": "bar",
        "categories": ["60以下", "60-69"], "values": [3, 8], "unit": "人",
    })
    assert bar is not None and bar["chart_type"] == "bar" and bar["unit"] == "人"


def test_chart_pie_scatter_heatmap() -> None:
    pie = build_figure({
        "type": "chart", "chart_type": "pie",
        "categories": ["A", "B"], "values": [3, 7],
    })
    assert pie is not None and pie["chart_type"] == "pie"

    scatter = build_figure({
        "type": "chart", "chart_type": "scatter",
        "points": [[1, 2], [2, 4], [3, 5]],
    })
    assert scatter is not None and len(scatter["points"]) == 3

    heat = build_figure({
        "type": "chart", "chart_type": "heatmap",
        "x_categories": ["一", "二"], "y_categories": ["甲", "乙"],
        "points": [[0, 0, 1], [1, 1, 5]],
    })
    assert heat is not None and heat["value_range"] == [1.0, 5.0]


def test_chart_rejects_thin_or_unknown_data() -> None:
    """图表数据不够就干脆不画：两三个数硬画成图，比讲一句更误导。"""
    assert build_figure({
        "type": "chart", "chart_type": "radar",
        "categories": ["A", "B"], "values": [1, 2],
    }) is None
    assert build_figure({
        "type": "chart", "chart_type": "line",
        "categories": ["只有一项"], "values": [1],
    }) is None
    assert build_figure({
        "type": "chart", "chart_type": "line",
        "categories": ["A", "B"], "values": [0, 0],
    }) is None
    assert build_figure({"type": "chart", "chart_type": "scatter", "points": [[1, 1]]}) is None
    assert build_figure({
        "type": "chart", "chart_type": "heatmap",
        "x_categories": ["A"], "y_categories": ["B", "C"],
        "points": [[0, 0, 1], [0, 1, 2]],
    }) is None


# ---------------------------------------------------------------- 面积图
def test_circle_and_triangle() -> None:
    circle = build_figure({"type": "circle", "r": 3})
    assert circle is not None
    assert circle["r"] == 3
    assert circle["radius_label"] == "r = 3"

    triangle = build_figure({"type": "triangle", "base": 6, "height": 4})
    assert triangle is not None
    assert triangle["vertices"]["B"][0] == 6
    assert triangle["vertices"]["C"][1] == 4


# ---------------------------------------------------------------- 坐标框
def test_rect_clamped_away_from_text_area() -> None:
    """绘图框必须留在右半区：越界就会被夹回，否则整张图压住讲解文字。"""
    fig = build_figure({
        "type": "flow", "nodes": ["a", "b"],
        "rect": [0.9, 0.9, 0.9, 0.9],
    })
    assert fig is not None
    x, y, w, h = fig["rect"]
    assert x >= 0.34, "绘图框不许盖到左侧文字"
    assert x + w <= 1.0 and y + h <= 1.0


# ---------------------------------------------------------------- 提示词键名
def test_numberline_and_geometry_accept_prompt_aliases() -> None:
    """提示词用 dots / vertices 举例：points 是讲课契约的字段名，答题栈不许出现。"""
    line = build_figure({
        "type": "numberline", "range": [-1, 5],
        "dots": [{"at": 2, "hollow": True, "label": "不含 2"}],
    })
    assert line is not None
    assert any(m["kind"] == "point" and m["at"][0] == 2 for m in line["marks"])

    geom = build_figure({"type": "geometry", "vertices": [[0, 0], [4, 0], [0, 3]]})
    assert geom is not None
    assert set(geom["vertices"]) == {"A", "B", "C"}


def test_answer_contract_and_skill_skeletons_avoid_lecture_fields() -> None:
    """答题侧的示例不能出现 ``points``，否则契约隔离测试会把它当成课件契约泄漏。"""
    from agents.shared.teaching_layers import ANSWER_CONTRACT
    from core.skills.agent_skills.builtin import _FIGURE_RULES

    assert '"points"' not in ANSWER_CONTRACT
    for _keys, kind, skeleton, _why in _FIGURE_RULES:
        assert '"points"' not in skeleton, f"{kind} 骨架用了讲课契约的字段名"


# ---------------------------------------------------------------- 白板装配
def test_whiteboard_figure_returns_op() -> None:
    op = Whiteboard().figure({"type": "circle", "r": 2})
    assert op is not None
    assert op.op == BoardOp.FIGURE
    assert op.meta["figure"]["type"] == "circle"
    # 图不可用 / 没给：返回 None，讲义继续、不出图
    assert Whiteboard().figure({"type": "unknown"}) is None
    assert Whiteboard().figure(None) is None

    chart = Whiteboard().figure({
        "type": "chart", "chart_type": "bar",
        "categories": ["A", "B"], "values": [1, 2],
    })
    assert chart is not None and chart.meta["figure"]["chart_type"] == "bar"


# ---------------------------------------------------------------- 校验
def test_validator_drops_figure_with_bad_rect() -> None:
    good = Whiteboard().figure({"type": "circle", "r": 2})
    bad = Whiteboard().figure({"type": "circle", "r": 2})
    assert good is not None and bad is not None
    bad.meta["figure"]["rect"] = [1.2, 0.1, 0.3, 0.3]

    resp = AIResponse(speech="我们来看这张图。", board_ops=[good, bad])
    dropped = drop_invalid_board_ops(resp)
    assert dropped == 1
    assert len(resp.board_ops) == 1

    result = ResponseValidator().validate(AIResponse(speech="讲解一下这张图。", board_ops=[bad]))
    assert "board_figure_out_of_range" in result.reasons


# ---------------------------------------------------------------- 链路
class _FakeLLM:
    """只满足构造要求：``_compose_response`` 不调模型。"""

    async def chat(self, *args, **kwargs):  # noqa: D401 - 测试替身
        return "{}"

    async def chat_stream(self, *args, **kwargs):
        if False:  # pragma: no cover - 让它是异步生成器
            yield ""


def test_teacher_compose_appends_figure_after_text() -> None:
    """配图必须挂在文字之后（前端收到新批次会先清板，同批下发才不会丢）。"""
    from agents.teacher.agent import TeacherAgent

    reply = json.dumps({
        "speech": "看这条抛物线，顶点就是最低点。",
        "title": "二次函数的最小值",
        "steps": ["配方得到顶点式", "读出顶点坐标"],
        "knowledge_points": ["顶点式 y=a(x-h)²+k"],
        "pitfalls": ["别忘了平方"],
        "figure": {"type": "function", "expr": "x*x-2*x-3", "x_range": [-2, 4]},
    }, ensure_ascii=False)

    resp = TeacherAgent(llm=_FakeLLM())._compose_response(reply)
    assert resp.board_ops, "至少有板书文字"
    assert resp.board_ops[-1].op == BoardOp.FIGURE
    assert resp.board_ops[-1].meta["figure"]["type"] == "function"


def test_teacher_compose_without_figure_stays_text_only() -> None:
    from agents.teacher.agent import TeacherAgent

    reply = json.dumps({
        "speech": "这道题先通分。",
        "title": "分数加法",
        "steps": ["找公分母", "分子相加"],
        "figure": None,
    }, ensure_ascii=False)

    resp = TeacherAgent(llm=_FakeLLM())._compose_response(reply)
    assert resp.board_ops
    assert all(op.op != BoardOp.FIGURE for op in resp.board_ops)
