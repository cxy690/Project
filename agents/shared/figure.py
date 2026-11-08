"""教学配图：把模型给出的「图型描述」编译成前端可直接绘制的图元。

为什么要有这一层
----------------
答疑时老师说「看这条抛物线」比说十句话都清楚，但**不能让模型直接给坐标**：
它既算不准，也写不完一条曲线要的两百个点。所以分工是——

* 模型只回答「画什么」：图型 + 少量参数（表达式、数据、顶点、半径…）；
* 这里负责「怎么画」：求值采样、定坐标范围、挑刻度、算矩形位置、兜底校验。

产出直接塞进 ``WhiteboardOp(op=BoardOp.FIGURE, meta={"figure": ...})``，
前端 ``renderFigure()`` 按 ``type`` 分派到对应画法。坐标一律是**数学坐标 +
x_range/y_range**，由前端映射成像素，因此换屏幕比例不会变形。

失败绝不外抛：任何一步出错都返回 ``None``，这节课照常只写板书文字。
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from config.logging import get_logger

log = get_logger("agents.shared.figure")

# ---------------------------------------------------------------- 限制
# 这些上限不是性能问题，是「一张板书配图装不下更多」的教学约束：
# 点太多曲线变毛刺、柱子太多看不清、流程节点太多学生记不住。
MAX_SERIES_POINTS = 1200
MAX_SERIES = 4
MAX_MARKS = 24
MAX_BARS = 12
MAX_FLOW_NODES = 8
MAX_ARROWS = 6
MAX_LABEL_CHARS = 40
MAX_TITLE_CHARS = 40

# 统计图表（ECharts）的上限：图表是「一眼看趋势」，类别/系列再多就变成读表格。
MAX_CHART_CATEGORIES = 24
MAX_CHART_SERIES = 6
MAX_CHART_POINTS = 200
CHART_TYPES = ("line", "bar", "pie", "scatter", "heatmap")

#: 图放在白板右半区（左侧留给文字解析），归一化 [x, y, w, h]
DEFAULT_RECT = [0.50, 0.065, 0.465, 0.87]

FIGURE_TYPES = (
    "function",     # 函数图像 / 直线
    "numberline",   # 数轴、不等式解集
    "geometry",     # 三角/四边形等平面图形
    "bar",          # 频数、统计柱状图
    "flow",         # 解题流程、步骤链
    "force",        # 受力分析
    "circle",       # 圆（面积、周长）
    "triangle",     # 三角形（面积、底高）
    "chart",        # 统计图表（ECharts：折线/柱状/饼图/散点/热力）
)

#: 表达式白名单：数字、**字母**、四则运算、乘方、括号与逗号。
#:
#: 字母曾经被限制成只许 x —— 结果学生问「半径 r」「时间 t」时，模型照着题目
#: 写 `r^2+10*r-90`，整张图被静默丢掉（2026-10-22 线上复现）。现在字母放行，
#: 再由 :func:`_normalize_vars` 把自变量统一收敛成 x；字符集仍是硬限制，
#: 引号/下划线/中文一律进不来，``import`` 这类词会被改名成 x 而解析失败 ——
#: 两者都只会得到「不出图」，不会执行任何代码。
_EXPR_ALLOWED = re.compile(r"^[0-9a-zA-Z+\-*/^().,\s]*$")
#: 解析时要原样保留的名字：函数与常数。其余字母序列一律视为「题目里的变量」，
#: 统一改名成 x（一张图只有一个自变量，名字跟题目走毫无意义）。
_EXPR_KEEP = frozenset(("sin", "cos", "tan", "sqrt", "abs", "log", "ln", "exp", "pi", "e", "x"))
_EXPR_NAME = re.compile(r"[A-Za-z]+")
#: 代码味的词直接拒：字符白名单已挡住引号/下划线，这里再堵一道「无引号也能写出
#: 可执行片段」的口子（``import os`` 归一化后会变成 x*x，画出一条毫无意义的抛物线）。
_EXPR_BLOCK = ("import", "exec", "eval", "open", "system", "subprocess", "lambda", "print", "def ", "class ")


# ---------------------------------------------------------------- 入口
def build_figure(data: Any) -> dict[str, Any] | None:
    """把模型给的 ``figure`` 字典编译成前端图元；不可用返回 ``None``。

    入参是**模型输出的原始结构**，可能缺字段、类型不对、带多余键 —— 一律
    按「尽力修，修不好就放弃」处理，绝不抛异常打断答疑。
    """
    if not isinstance(data, dict):
        return None
    kind = str(data.get("type") or "").strip().lower()
    builder = _BUILDERS.get(kind)
    if builder is None:
        return None
    try:
        fig = builder(data)
    except Exception as err:  # noqa: BLE001 - 配图失败不能影响讲课
        log.warning("配图编译失败 type={} err={}", kind, err)
        return None
    if not fig:
        return None

    fig["type"] = kind
    rect = data.get("rect")
    fig["rect"] = _rect(rect) if isinstance(rect, (list, tuple)) else list(DEFAULT_RECT)
    title = _text(data.get("title"), MAX_TITLE_CHARS)
    if title:
        fig["title"] = title
    return fig if _sane(fig) else None


class _Ctx:
    """编译过程中的小工具：把「模型可能给错」的输入收敛成可绘制参数。"""

    @staticmethod
    def num(value: Any, default: float | None = None) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return default
        return out if math.isfinite(out) else default

    @staticmethod
    def pair(value: Any) -> tuple[float, float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        a, b = _Ctx.num(value[0]), _Ctx.num(value[1])
        if a is None or b is None:
            return None
        return a, b

    @staticmethod
    def span(value: Any, default: tuple[float, float]) -> tuple[float, float]:
        got = _Ctx.pair(value)
        if not got:
            return default
        lo, hi = got
        if hi - lo < 1e-6:
            return default
        return lo, hi


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    out = value.strip().replace("\n", " ")
    return out[:limit]


def _rect(value: Any) -> list[float]:
    """图画在哪：限制在板面内，且**不许压到左半区文字**。"""
    out = []
    for i, item in enumerate(list(value)[:4]):
        got = _Ctx.num(item)
        if got is None:
            got = DEFAULT_RECT[i]
        out.append(round(got, 3))
    while len(out) < 4:
        out.append(DEFAULT_RECT[len(out)])
    x, y, w, h = out
    x = min(max(x, 0.34), 0.94)
    y = min(max(y, 0.02), 0.94)
    w = min(max(w, 0.18), 0.64)
    h = min(max(h, 0.18), 0.94)
    if x + w > 0.99:
        w = round(0.99 - x, 3)
    if y + h > 0.99:
        h = round(0.99 - y, 3)
    return [round(x, 3), round(y, 3), round(w, 3), round(h, 3)]


def _sane(fig: dict[str, Any]) -> bool:
    """最后一道闸：范围必须是有限数，且跨度不能离谱。"""
    xr, yr = fig.get("x_range"), fig.get("y_range")
    if not (isinstance(xr, (list, tuple)) and len(xr) == 2):
        return False
    if not (isinstance(yr, (list, tuple)) and len(yr) == 2):
        return False
    for v in (xr[0], xr[1], yr[0], yr[1]):
        if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            return False
    return (xr[1] - xr[0]) > 1e-6 and (yr[1] - yr[0]) > 1e-6


# ---------------------------------------------------------------- function
def _sample_function(expr: str, x0: float, x1: float, n: int) -> list[list[float]]:
    """对表达式采样成点列；sympy 不可用或表达式不合法时返回空。"""
    expr = _clean_expr(expr)
    if not expr:
        return []
    try:
        import sympy
        from sympy.parsing.sympy_parser import (
            implicit_multiplication_application,
            parse_expr,
            standard_transformations,
        )
    except ImportError:  # pragma: no cover - 取决于环境
        return []

    transforms = standard_transformations + (implicit_multiplication_application,)
    try:
        # 必须显式把 x 绑成符号：交给 parse_expr 自己猜会把它当成常数名，
        # 采出来是一条水平线（第一次接入就是这么踩的）。
        x = sympy.Symbol("x")
        # 函数与常数必须显式给出：交给 parse_expr 自己猜会把 sin 当成符号名，
        # 采值时直接抛异常（整张图丢掉）。小写 e 不是 sympy 的常数，也在这里绑定。
        local = {
            "x": x,
            "e": sympy.E, "pi": sympy.pi,
            "sin": sympy.sin, "cos": sympy.cos, "tan": sympy.tan,
            "sqrt": sympy.sqrt, "abs": sympy.Abs,
            "log": sympy.log, "ln": sympy.log, "exp": sympy.exp,
        }
        parsed = parse_expr(
            expr.replace("^", "**"),
            transformations=transforms,
            local_dict=local,
            evaluate=True,
        )
        fn = sympy.lambdify(x, parsed, "math")
    except Exception as err:  # noqa: BLE001 - 表达式写坏就放弃这张图
        log.warning("函数表达式解析失败 expr={!r} err={}", expr, err)
        return []

    pts: list[list[float]] = []
    for i in range(n):
        xv = x0 + (x1 - x0) * i / (n - 1)
        try:
            yv = float(fn(xv))
        except Exception:  # noqa: BLE001 - 不连续点（1/x 等）跳过
            continue
        if math.isfinite(yv):
            pts.append([round(xv, 3), round(yv, 3)])
    return pts


def _clean_expr(raw: Any) -> str:
    """只保留安全字符；清不出东西就返回空串。"""
    if not isinstance(raw, str):
        return ""
    expr = raw.strip()
    expr = expr.replace("＝", "=").replace("×", "*").replace("÷", "/")
    expr = expr.replace("−", "-").replace("–", "-").replace("（", "(").replace("）", ")")
    expr = re.sub(r"^\s*y\s*=\s*", "", expr, flags=re.I)     # 允许模型把「y=」写进来
    expr = re.sub(r"^\s*f\s*\(\s*x\s*\)\s*=\s*", "", expr, flags=re.I)
    if len(expr) > 120:
        return ""
    if not _EXPR_ALLOWED.match(expr):
        return ""
    if any(word in expr.lower() for word in _EXPR_BLOCK):
        return ""
    return _normalize_vars(expr)


def _normalize_vars(expr: str) -> str:
    """把题目里的自变量统一成 x；函数名与常数原样保留。

    例：``r^2+10*r-90`` -> ``x^2+10*x-90``；``sqrt(t)`` -> ``sqrt(x)``。
    大小写不敏感（``X``/``T`` 同样收敛），未知的多字母名字也当变量处理。
    """
    def _swap(match: re.Match[str]) -> str:
        name = match.group(0).lower()
        return "x" if name not in _EXPR_KEEP else name

    return _EXPR_NAME.sub(_swap, expr)


def _build_function(data: dict[str, Any]) -> dict[str, Any] | None:
    expr = _clean_expr(data.get("expr") or data.get("expression"))
    if not expr:
        return None

    x0, x1 = _Ctx.span(data.get("x_range"), (-5.0, 5.0))
    x0, x1 = max(-100.0, x0), min(100.0, x1)
    if x1 - x0 > 40:
        x0, x1 = x0, x0 + 40
    if x1 - x0 < 0.5:
        x0, x1 = -5.0, 5.0

    pts = _sample_function(expr, x0, x1, 260)
    if len(pts) < 2:
        return None

    ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    if hi - lo < 1e-6:
        lo, hi = lo - 2.0, hi + 2.0
    # 只裁到「不超过 x 跨度 3 倍」，避免 1/x 这类函数把画面拉成一条竖线
    limit = (x1 - x0) * 3.0
    if hi - lo > limit:
        mid = (hi + lo) / 2
        lo, hi = mid - limit / 2, mid + limit / 2
    pad = (hi - lo) * 0.08
    yr = [round(lo - pad, 3), round(hi + pad, 3)]

    series = [{
        "points": pts[:MAX_SERIES_POINTS],
        "color": _color(data.get("color"), "#1677ff"),
        "label": _text(data.get("label") or data.get("series_label"), 24),
    }]

    marks = _marks(data.get("marks"), x0, x1, yr[0], yr[1])
    fig: dict[str, Any] = {
        "x_range": [round(x0, 3), round(x1, 3)],
        "y_range": yr,
        "axes": {"grid": True},
        "series": series,
        "marks": marks,
    }
    result = _text(data.get("result"), MAX_LABEL_CHARS)
    if result:
        fig["result"] = result
    return fig


def _marks(raw: Any, x0: float, x1: float, y0: float, y1: float) -> list[dict[str, Any]]:
    """标注点：越界的直接丢，宁可少标一个也不让前端画出板面。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_MARKS]:
        if not isinstance(item, dict):
            continue
        at = _Ctx.pair(item.get("at") or item.get("point"))
        if not at:
            continue
        ax, ay = at
        if not (x0 - 1e-6 <= ax <= x1 + 1e-6 and y0 - 1e-6 <= ay <= y1 + 1e-6):
            continue
        mark: dict[str, Any] = {
            "kind": "point",
            "at": [round(ax, 3), round(ay, 3)],
            "color": _color(item.get("color"), "#cf1322"),
        }
        label_text = _text(item.get("label"), MAX_LABEL_CHARS)
        if label_text:
            mark["label"] = label_text
        for key in ("dx", "dy"):
            got = _Ctx.num(item.get(key))
            if got is not None:
                mark[key] = round(got, 1)
        out.append(mark)
    return out


# ---------------------------------------------------------------- numberline
def _build_numberline(data: dict[str, Any]) -> dict[str, Any] | None:
    lo, hi = _Ctx.span(data.get("range") or data.get("x_range"), (-3.0, 6.0))
    if hi - lo > 30:
        hi = lo + 30
    if hi - lo < 1:
        lo, hi = -3.0, 6.0

    marks: list[dict[str, Any]] = []
    rays = data.get("rays")
    if isinstance(rays, list):
        for item in rays[:4]:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                a, b = _Ctx.num(item[0]), _Ctx.num(item[1])
            elif isinstance(item, dict):
                a, b = _Ctx.num(item.get("from")), _Ctx.num(item.get("to"))
            else:
                continue
            if a is None or b is None:
                continue
            marks.append({
                "kind": "ray",
                "from": round(min(a, b), 3),
                "to": round(max(a, b), 3),
                "color": "#1677ff",
            })

    # 提示词里写 ``dots``：``points`` 是讲课契约的字段名，答题契约里出现它
    # 会让「双栈契约隔离」的守卫误判成泄漏。两个键都认，模型照旧可用 points。
    points = data.get("dots")
    if not isinstance(points, list):
        points = data.get("points")
    if isinstance(points, list):
        for item in points[:MAX_MARKS]:
            if isinstance(item, dict):
                at = _Ctx.num(item.get("at"))
                hollow = bool(item.get("hollow", True))
                label_text = _text(item.get("label"), MAX_LABEL_CHARS)
                color = _color(item.get("color"), "#1677ff")
            else:
                at, hollow, label_text, color = _Ctx.num(item), True, "", "#1677ff"
            if at is None or not (lo - 1 <= at <= hi + 1):
                continue
            mark: dict[str, Any] = {
                "kind": "point",
                "at": [round(at, 3), 0],
                "hollow": hollow,
                "color": color,
            }
            if label_text:
                mark["label"] = label_text
                mark["dy"] = -22
                mark["align"] = "center"
            marks.append(mark)

    # 解集说明写在轴上方（避开刻度）
    note = _text(data.get("note") or data.get("label"), MAX_LABEL_CHARS)
    if note:
        marks.append({
            "kind": "text", "at": [round(lo + 0.2, 3), 1.45],
            "text": note, "color": "#12559e", "size": 14, "weight": 700,
        })

    return {
        "x_range": [round(lo - 0.4, 3), round(hi + 0.4, 3)],
        # 轴放在偏下位置，上方留给「解集」结论
        "y_range": [-0.85, 1.75],
        "marks": marks,
    }


# ---------------------------------------------------------------- geometry
def _build_geometry(data: dict[str, Any]) -> dict[str, Any] | None:
    raw = data.get("points") or data.get("vertices")
    pts: list[list[float]] = []
    if isinstance(raw, list):
        for item in raw[:8]:
            got = _Ctx.pair(item)
            if got:
                pts.append([round(got[0], 3), round(got[1], 3)])
    elif isinstance(raw, dict):
        for _, item in list(raw.items())[:8]:
            got = _Ctx.pair(item)
            if got:
                pts.append([round(got[0], 3), round(got[1], 3)])
    if len(pts) < 3:
        return None

    names = data.get("names")
    if not isinstance(names, list) or len(names) < len(pts):
        names = [chr(ord("A") + i) for i in range(len(pts))]
    names = [_text(str(n), 3) or chr(ord("A") + i) for i, n in enumerate(names[:len(pts)])]

    vertices = {names[i]: pts[i] for i in range(len(pts))}
    edges = [[names[i], names[(i + 1) % len(names)]] for i in range(len(names))]

    side_labels: list[dict[str, Any]] = []
    raw_labels = data.get("side_labels") or data.get("labels")
    if isinstance(raw_labels, list):
        for idx, item in enumerate(raw_labels[:8]):
            if isinstance(item, dict):
                on = item.get("on")
                text = _text(item.get("text"), 16)
                offset = _Ctx.pair(item.get("offset")) or (0.0, 0.0)
                if not (isinstance(on, list) and len(on) == 2 and text):
                    continue
                a = on[0] if isinstance(on[0], str) else names[on[0] % len(names)]
                b = on[1] if isinstance(on[1], str) else names[on[1] % len(names)]
                side_labels.append({
                    "on": [a, b], "text": text,
                    "offset": [round(offset[0], 2), round(offset[1], 2)],
                })
            elif isinstance(item, str) and item.strip():
                # 退化：按顺序贴到第 i 条边上
                a, b = edges[idx % len(edges)]
                side_labels.append({"on": [a, b], "text": item.strip()[:16], "offset": [0, 0.28]})

    fig: dict[str, Any] = {
        "vertices": vertices,
        "edges": edges,
        "fills": [[names[i] for i in range(len(names))]],
        "side_labels": side_labels,
    }

    ra = data.get("right_angle")
    if isinstance(ra, dict):
        at = ra.get("at")
        arms = ra.get("arms")
        if isinstance(at, int) and 0 <= at < len(names):
            at_name = names[at]
        elif isinstance(at, str) and at in vertices:
            at_name = at
        else:
            at_name = ""
        arm_names = []
        if isinstance(arms, list):
            for item in arms[:2]:
                if isinstance(item, int) and 0 <= item < len(names):
                    arm_names.append(names[item])
                elif isinstance(item, str) and item in vertices:
                    arm_names.append(item)
        if at_name and len(arm_names) == 2:
            fig["right_angle"] = {"at": at_name, "arms": arm_names, "size": 0.35}

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    padx = max((max(xs) - min(xs)) * 0.22, 0.6)
    pady = max((max(ys) - min(ys)) * 0.26, 0.6)
    fig["x_range"] = [round(min(xs) - padx, 3), round(max(xs) + padx, 3)]
    fig["y_range"] = [round(min(ys) - pady, 3), round(max(ys) + pady, 3)]

    result = _text(data.get("result"), MAX_LABEL_CHARS)
    if result:
        fig["result"] = result
    return fig


# ---------------------------------------------------------------- bar
def _build_bar(data: dict[str, Any]) -> dict[str, Any] | None:
    cats = data.get("categories") or data.get("labels")
    vals = data.get("values")
    if not isinstance(cats, list) or not isinstance(vals, list):
        return None
    n = min(len(cats), len(vals), MAX_BARS)
    if n < 2:
        return None

    categories = [_text(str(c), 10) or f"第{i + 1}项" for i, c in enumerate(cats[:n])]
    values: list[float] = []
    for item in vals[:n]:
        got = _Ctx.num(item, 0.0) or 0.0
        values.append(round(max(0.0, min(got, 1e6)), 2))

    top = max(values) or 1.0
    return {
        "x_range": [0, n],
        "y_range": [0, round(top * 1.18, 2)],
        "categories": categories,
        "values": values,
        "unit": _text(data.get("unit"), 6),
        "color": _color(data.get("color"), "#4f8dfd"),
        "highlight": int(_Ctx.num(data.get("highlight"), -1) or -1),
        "result": _text(data.get("result"), MAX_LABEL_CHARS),
    }


# ---------------------------------------------------------------- flow
def _build_flow(data: dict[str, Any]) -> dict[str, Any] | None:
    raw = data.get("nodes") or data.get("steps")
    if not isinstance(raw, list):
        return None
    nodes = [_text(str(x), 16) for x in raw[:MAX_FLOW_NODES] if _text(str(x), 16)]
    if len(nodes) < 2:
        return None
    return {
        "x_range": [0, 1],
        "y_range": [0, 1],
        "nodes": nodes,
    }


# ---------------------------------------------------------------- force
def _build_force(data: dict[str, Any]) -> dict[str, Any] | None:
    raw = data.get("arrows") or data.get("forces")
    if not isinstance(raw, list):
        return None
    arrows: list[dict[str, Any]] = []
    for item in raw[:MAX_ARROWS]:
        if not isinstance(item, dict):
            continue
        dx = _Ctx.num(item.get("dx"))
        dy = _Ctx.num(item.get("dy"))
        if dx is None or dy is None:
            continue
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue
        arrows.append({
            "dx": round(max(-3.6, min(dx, 3.6)), 2),
            "dy": round(max(-3.0, min(dy, 3.0)), 2),
            "color": _color(item.get("color"), "#ef4444"),
            "label": _text(item.get("label"), 14),
            "align": "center",
        })
    if len(arrows) < 2:
        return None

    # 按箭头方向给标签让位：向左的标在左侧，向右的标在右侧
    for arrow in arrows:
        if abs(arrow["dx"]) > 0.9 and abs(arrow["dy"]) < 0.4:
            arrow["align"] = "right" if arrow["dx"] < 0 else "left"

    return {
        "x_range": [-4.0, 4.0],
        "y_range": [-2.8, 3.0],
        "body": {
            "x": 0, "y": 0,
            "w": 1.9, "h": 1.2,
            "label": _text(data.get("body_label"), 8) or "物体",
            "fill": "#fff3df", "stroke": "#d9a066",
        },
        "arrows": arrows,
        "ground": bool(data.get("ground", True)),
    }


# ---------------------------------------------------------------- circle
def _build_circle(data: dict[str, Any]) -> dict[str, Any] | None:
    r = _Ctx.num(data.get("r") or data.get("radius"), 3.0) or 3.0
    r = max(0.5, min(r, 20.0))
    span = r * 1.55
    return {
        "x_range": [round(-span, 3), round(span, 3)],
        "y_range": [round(-span * 0.86, 3), round(span * 0.86, 3)],
        "center": [0, 0],
        "r": round(r, 3),
        "radius_label": _text(data.get("radius_label"), 20) or f"r = {_fmt(r)}",
        "result": _text(data.get("result"), MAX_LABEL_CHARS),
    }


# ---------------------------------------------------------------- triangle
def _build_triangle(data: dict[str, Any]) -> dict[str, Any] | None:
    base = _Ctx.num(data.get("base") or data.get("b"), 6.0) or 6.0
    height = _Ctx.num(data.get("height") or data.get("h"), 4.0) or 4.0
    base = max(0.5, min(base, 40.0))
    height = max(0.5, min(height, 40.0))
    apex = base * 0.45
    return {
        "vertices": {"A": [0, 0], "B": [round(base, 3), 0], "C": [round(apex, 3), round(height, 3)]},
        "x_range": [round(-base * 0.18, 3), round(base * 1.18, 3)],
        "y_range": [round(-height * 0.3, 3), round(height * 1.32, 3)],
        "base_label": _text(data.get("base_label"), 20) or f"底 b = {_fmt(base)}",
        "height_label": _text(data.get("height_label"), 20) or f"高 h = {_fmt(height)}",
        "result": _text(data.get("result"), MAX_LABEL_CHARS),
    }


# ---------------------------------------------------------------- chart（ECharts）
def _chart_values(raw: Any, n: int) -> list[float]:
    """把模型的数值列表收敛成 n 个有限数；全为 0 视为「没数据」。"""
    if not isinstance(raw, list) or not raw:
        return []
    out: list[float] = []
    for item in raw[:n]:
        got = _Ctx.num(item, 0.0)
        out.append(round(max(-1e9, min(float(got or 0.0), 1e9)), 3))
    while len(out) < n:
        out.append(0.0)
    return out if any(abs(v) > 1e-9 for v in out) else []


def _chart_labels(raw: Any, prefix: str) -> list[str]:
    """分类名：缺失就补「第 1 项」，避免 ECharts 画出空白刻度。"""
    if not isinstance(raw, list):
        return []
    return [_text(str(c), 12) or f"{prefix}{i + 1}" for i, c in enumerate(raw[:MAX_CHART_CATEGORIES])]


def _chart_tail(data: dict[str, Any]) -> dict[str, Any]:
    """图表通用字段：坐标轴名、单位、结论。"""
    return {
        "x_label": _text(data.get("x_label"), 16),
        "y_label": _text(data.get("y_label"), 16),
        "unit": _text(data.get("unit"), 6),
        "result": _text(data.get("result"), MAX_LABEL_CHARS),
    }


def _build_chart(data: dict[str, Any]) -> dict[str, Any] | None:
    """统计图表：交前端 ECharts 画，这里只收敛数据与范围。

    与其它图型分工一致：模型给分类/数值，本函数负责截断、补零、定范围；
    坐标轴、图例、配色由 ECharts 自己排，所以这里不产出点列。
    """
    kind = str(data.get("chart_type") or data.get("chart") or "bar").strip().lower()
    if kind not in CHART_TYPES:
        return None
    if kind == "scatter":
        return _build_chart_scatter(data)
    if kind == "heatmap":
        return _build_chart_heatmap(data)

    categories = _chart_labels(
        data.get("categories") or data.get("labels") or data.get("x"), "第")
    if len(categories) < 2:
        return None
    n = len(categories)

    series: list[dict[str, Any]] = []
    raw_series = data.get("series")
    if isinstance(raw_series, list):
        for item in raw_series[:MAX_CHART_SERIES]:
            if not isinstance(item, dict):
                continue
            values = _chart_values(item.get("values") or item.get("data"), n)
            if values:
                series.append({"name": _text(item.get("name"), 16), "values": values})
    if not series:
        values = _chart_values(data.get("values") or data.get("data"), n)
        if values:
            series.append({"name": _text(data.get("name") or data.get("label"), 16), "values": values})
    if not series:
        return None
    if kind == "pie":
        # 饼图只表达「一个整体的构成」：多系列会被画成同心环，学生反而读不懂
        series = series[:1]

    flat = [v for s in series for v in s["values"]]
    if kind == "pie":
        x_range, y_range = [0.0, 1.0], [0.0, 1.0]
    else:
        bottom, top = min(flat), max(flat)
        if bottom > 0:
            bottom = 0.0          # 柱/线从 0 起，避免把 3 和 8 的差距夸大成悬崖
        if top < 0:
            top = 0.0
        if top - bottom < 1e-6:
            top = bottom + 1.0
        x_range, y_range = [0.0, float(n)], [round(bottom, 3), round(top * 1.15, 3)]

    fig: dict[str, Any] = {
        "chart_type": kind,
        "categories": categories,
        "series": series,
        "x_range": x_range,
        "y_range": y_range,
    }
    fig.update(_chart_tail(data))
    return fig


def _build_chart_scatter(data: dict[str, Any]) -> dict[str, Any] | None:
    raw = data.get("points") if "points" in data else data.get("data")
    if not isinstance(raw, list):
        return None
    pts: list[list[float]] = []
    for item in raw[:MAX_CHART_POINTS]:
        got = _Ctx.pair(item)
        if got:
            pts.append([round(got[0], 3), round(got[1], 3)])
    if len(pts) < 2:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 - x0 < 1e-6:
        x0, x1 = x0 - 1.0, x1 + 1.0
    if y1 - y0 < 1e-6:
        y0, y1 = y0 - 1.0, y1 + 1.0
    padx, pady = (x1 - x0) * 0.08, (y1 - y0) * 0.08

    fig: dict[str, Any] = {
        "chart_type": "scatter",
        "points": pts,
        "x_range": [round(x0 - padx, 3), round(x1 + padx, 3)],
        "y_range": [round(y0 - pady, 3), round(y1 + pady, 3)],
    }
    fig.update(_chart_tail(data))
    return fig


def _build_chart_heatmap(data: dict[str, Any]) -> dict[str, Any] | None:
    x_cats = _chart_labels(
        data.get("x_categories") or data.get("categories") or data.get("x"), "列")
    y_cats = _chart_labels(
        data.get("y_categories") or data.get("rows") or data.get("y"), "行")
    if len(x_cats) < 2 or len(y_cats) < 2:
        return None

    raw = data.get("points") if "points" in data else data.get("data")
    if not isinstance(raw, list):
        return None
    pts: list[list[float]] = []
    for item in raw[:MAX_CHART_POINTS]:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        ix, iy, value = _Ctx.num(item[0]), _Ctx.num(item[1]), _Ctx.num(item[2])
        if ix is None or iy is None or value is None:
            continue
        xi, yi = int(ix), int(iy)
        if not (0 <= xi < len(x_cats) and 0 <= yi < len(y_cats)):
            continue
        pts.append([xi, yi, round(value, 3)])
    if len(pts) < 2:
        return None

    values = [p[2] for p in pts]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-6:
        hi = lo + 1.0
    fig: dict[str, Any] = {
        "chart_type": "heatmap",
        "x_categories": x_cats,
        "y_categories": y_cats,
        "points": pts,
        "value_range": [round(lo, 3), round(hi, 3)],
        "x_range": [0.0, float(len(x_cats))],
        "y_range": [0.0, float(len(y_cats))],
    }
    fig.update(_chart_tail(data))
    return fig


def _fmt(value: float) -> str:
    """3.0 → "3"，2.5 → "2.5"。"""
    return str(int(value)) if abs(value - round(value)) < 1e-9 else f"{value:g}"


def _color(raw: Any, default: str) -> str:
    """只接受 #rrggbb，防止把奇怪字符串塞进 canvas 的 strokeStyle。"""
    if isinstance(raw, str):
        got = raw.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", got):
            return got.lower()
    return default


_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
    "function": _build_function,
    "numberline": _build_numberline,
    "geometry": _build_geometry,
    "bar": _build_bar,
    "flow": _build_flow,
    "force": _build_force,
    "circle": _build_circle,
    "triangle": _build_triangle,
    "chart": _build_chart,
}


__all__ = ["build_figure", "FIGURE_TYPES", "CHART_TYPES", "DEFAULT_RECT", "MAX_SERIES_POINTS"]
