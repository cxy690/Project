"""教学图规格（spec）：让模型把一道题变成「可以逐笔画出」的 JSON，并做结构与安全校验。

为什么单独一层
--------------
渲染完全在浏览器里（``clients/vendor/teach-diagram/engine.js``），Python 只产数据。
这样画图**不需要 node、子进程、iframe、图片转码**——规格几十 KB 的 JSON 走现有的
WebSocket 通道就下去了，前端直接 ``TeachDiagram.mount(spec)``。少一个运行时，
就少一处能在课堂上卡住的地方。

三条硬约束（与 ``core/planning/planner.py`` 同一套价值观）
--------------------------------------------------------
1. **画图不能拖慢上课**：本模块任何失败（超时 / 无密钥 / 模型乱写 / 表达式非法）
   一律返回 ``None``，调用方照常讲课，只是这一轮没有图；
2. **规格必须是前端能直接吃的**：所以这里做归一化——补齐 id、把 ``steps.reveal``
   对齐到真实存在的 id、校验函数表达式语法、夹取坐标；
3. **结果要能复用**：同一道题（题干文本相同）第二次提问直接读缓存，不再调模型。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import settings
from models.base import BaseLLMClient
from models.llm import get_llm

log = get_logger("core.diagram.spec")

#: 引擎支持的图型（必须与 engine.js 的 TYPES 一致，否则 mount 会直接抛错）
TYPES: tuple[str, ...] = (
    "flow", "concept", "timeline", "compare", "curve", "cycle", "layers", "board",
)

#: 画布固定横屏：学生端白板就是横的，竖屏 specs 放进去会左右大片留白
CANVAS_W, CANVAS_H = 1920, 1080

#: board 的可用区域（给标题区、白板字幕条留边）——模型爱把坐标画到边上
BOARD_X = (120.0, 1800.0)
BOARD_Y = (200.0, 960.0)

#: 表达式里允许出现的标识符（函数名 + 变量 + 常数），其余一律拒绝
_EXPR_TOKENS = frozenset({
    "x", "pi", "e",
    "sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs",
    "exp", "ln", "log", "floor", "ceil", "round",
})
_EXPR_CHARS = re.compile(r"^[0-9A-Za-z+\-*/^().,\s]+$")
_WORD = re.compile(r"[A-Za-z]+")

#: id 规范：字母开头，字母数字下划线短横，长度受限（会被写进 DOM 属性，别太野）
_ID_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")

_PROMPT = """你是教学插图规划器。把下面这道题变成一张「可以逐笔画出、能配合讲解分步揭示」的示意图。

只输出一个 JSON 对象，不要任何解释文字。骨架：
{{"title": "不超过 14 字",
 "type": "curve|board|flow|concept|compare|layers|timeline|cycle",
 "canvas": "landscape",
 "theme": "light",
 "steps": [{{"reveal": ["id"], "narration": "不超过 30 字"}}],
 ...按 type 补图元数组...}}

选型判据（严格按顺序判断，命中就停）：
1. 题里有函数解析式 / 一次函数 / 二次函数 / 三角函数 / 反比例函数 / 图像性质 → "curve"。
   曲线只写表达式 fn（如 "x^2-2x-3"、"sin(x)"、"4/x"、"exp(-x^2)"），**不要自己算采样点**；
2. 平面几何作图（三角形、圆、平行线、垂线、辅助线、坐标系里描点连线）→ "board"。
   画布 1920x1080，绝对坐标，y 向下增大；内容留在 x∈[140,1780]、y∈[200,960]；
3. 应用题的数量关系、解题步骤有先后 → "flow"；
4. 一个概念拆成要点 → "concept"；两组对象逐条对照 → "compare"；
   有年代/先后发展 → "timeline"；首尾相接的闭环 → "cycle"；上下分层 → "layers"。

各类型图元字段（只写用到的）：
- curve: axes{{"style":"cross","x":{{"min":-2,"max":4,"label":"x"}},"y":{{"min":-4,"max":5,"label":"y"}}}},
  curves[{{"id","label","fn"}}], markers[{{"id","x","y","text"}}],
  guides[{{"id","type":"v|h","at","label"}}], regions[{{"id","from","to","label"}}]
- board: strokes[{{"id","points":[[x,y],...],"color","width"}}], circles[{{"id","x","y","r"}}],
  arrows[{{"id","from":[x,y],"to":[x,y]}}], notes[{{"id","x","y","text","size"}}]
- flow: nodes[{{"id","text","shape"}}], edges[{{"from","to","label"}}]
- concept: center{{"id","text"}}, branches[{{"id","text"}}]
- compare: left{{"title","items":[{{"id","text"}}]}}, right{{"title","items":[{{"id","text"}}]}},
  conclusion{{"id","text"}}
- layers: layers[{{"id","label","detail"}}]
- timeline: events[{{"id","date","text"}}]
- cycle: nodes[{{"id","text"}}], center_text

硬规则：
1. id 一律用英文小写短词（leg_a / vertex / root1），全图唯一；
2. steps 写 3~5 步，顺序 = 讲课顺序，先轮廓后细节；
   每步 reveal 1~3 个 id，且**只能引用上面真的出现过的 id**；
3. 每个图元都必须至少出现在某一步的 reveal 里，否则它在演示中根本不会出现；
4. 文本不超过 12 字，要换行写 \\n；颜色只用 "a1" "a2" "a3" "a4"；
5. 不要编造题目里没有的数据（数字、年份、比例必须来自题干）。

题目：
{question}
"""


class DiagramSpecBuilder:
    """题目 → 教学图规格（模型优先，失败即 ``None``）。"""

    def __init__(
        self,
        llm: BaseLLMClient | None = None,
        *,
        timeout: float | None = None,
        max_tokens: int | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self._llm = llm
        self.timeout = float(
            timeout if timeout is not None else settings.diagram_timeout_seconds
        )
        self.max_tokens = int(
            max_tokens if max_tokens is not None else settings.diagram_max_tokens
        )
        self.cache_dir = Path(cache_dir or settings.diagram_cache_dir)

    @property
    def llm(self) -> BaseLLMClient:
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ---------------------------------------------------------------- 对外
    async def build(self, question: str, *, subject: str = "") -> dict[str, Any] | None:
        """产出一份可直接交给前端 mount 的规格；任何异常都返回 ``None``。"""
        text = _clean_question(question)
        if not text:
            return None
        cached = await self._cache_get(text)
        if cached is not None:
            log.info("教学图命中缓存：{}", cached.get("title") or "-")
            return cached
        if self._llm is None and not settings.llm_api_key:
            # 无密钥 = MockLLM，它产不出可用规格，省掉这次调用（课堂上白等 8 秒最亏）
            return None
        try:
            raw = await asyncio.wait_for(
                self.llm.chat_json(
                    [{"role": "user", "content": _PROMPT.format(question=text[:1200])}],
                    temperature=0.2,
                    max_tokens=self.max_tokens,
                ),
                timeout=self.timeout,
            )
        except Exception as err:  # noqa: BLE001 - 画图失败绝不能影响讲课
            log.warning("教学图规格生成失败（本轮不画图）：{}", err)
            return None
        spec = sanitize(raw, subject=subject)
        if spec is None:
            log.info("教学图规格不可用（模型输出未通过校验），本轮不画图")
            return None
        await self._cache_put(text, spec)
        log.info("教学图就绪：{} | {} | {} 步",
                 spec.get("type"), spec.get("title") or "-", len(spec.get("steps") or []))
        return spec

    # ---------------------------------------------------------------- 缓存
    def _cache_path(self, question: str) -> Path:
        digest = hashlib.sha1(question.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    async def _cache_get(self, question: str) -> dict[str, Any] | None:
        path = self._cache_path(question)
        try:
            if not path.exists():
                return None
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            data = json.loads(raw)
        except Exception as err:  # noqa: BLE001 - 缓存坏了等于没缓存
            log.debug("教学图缓存读取失败（忽略）：{}", err)
            return None
        return data if isinstance(data, dict) and data.get("type") in TYPES else None

    async def _cache_put(self, question: str, spec: dict[str, Any]) -> None:
        path = self._cache_path(question)
        try:
            await asyncio.to_thread(self.cache_dir.mkdir, parents=True, exist_ok=True)
            payload = json.dumps(spec, ensure_ascii=False)
            await asyncio.to_thread(path.write_text, payload, "utf-8")
        except Exception as err:  # noqa: BLE001 - 写缓存失败不影响本轮
            log.debug("教学图缓存写入失败（忽略）：{}", err)


# ==================================================================== 校验 / 归一化

def sanitize(raw: Any, *, subject: str = "") -> dict[str, Any] | None:
    """把模型输出归一化成引擎认得的规格；不可用时返回 ``None``。

    模型最常见的三类错：图元没写 id、``reveal`` 引用了不存在的 id、函数表达式写错。
    前两类这里自动修好（补 id / 丢无效引用 / 把漏掉的图元补进最后一步），
    第三类只能丢弃那条曲线（其余曲线照常画）。
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in TYPES:
        return None

    builder = _BUILDERS.get(kind)
    if builder is None:
        return None
    built = builder(raw)
    if built is None:
        return None
    spec, ids = built
    if not ids:
        return None

    spec["type"] = kind
    spec["canvas"] = "landscape"
    spec["theme"] = "light"          # 白板底色是白的，深色主题贴上去像一块补丁
    spec["title"] = _text(raw.get("title"), 28)
    subtitle = _text(raw.get("subtitle"), 36) or (f"{subject} · 讲解示意图" if subject else "")
    if subtitle:
        spec["subtitle"] = subtitle

    steps = _steps(raw.get("steps"), ids)
    if len(steps) < 2:
        # 分步没意义（或模型没给）：退回「一个图元一步」，引擎会自己按注册顺序分步
        spec.pop("steps", None)
    else:
        spec["steps"] = steps
    return spec


def _steps(raw: Any, ids: list[str]) -> list[dict[str, Any]]:
    """归一化分步脚本：丢掉不存在的 id，把没被揭示的图元补进最后一步。"""
    known = set(ids)
    out: list[dict[str, Any]] = []
    revealed: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        reveal = item.get("reveal")
        reveal = [reveal] if isinstance(reveal, str) else (reveal or [])
        clean = [str(x) for x in reveal if str(x) in known and str(x) not in revealed]
        if not clean:
            continue
        revealed.update(clean)
        step: dict[str, Any] = {"reveal": clean}
        narration = _text(item.get("narration"), 60)
        if narration:
            step["narration"] = narration
        out.append(step)
    if not out:
        return []
    missing = [x for x in ids if x not in revealed]
    if missing:
        # 没被任何一步揭示的图元在演示里永远不出现——补到最后一步，
        # 宁可最后一步多亮两块，也不要让图上少一条边。
        out[-1]["reveal"] = list(out[-1]["reveal"]) + missing
    return out[:6]


# ---------------------------------------------------------------- 各图型

def _build_curve(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    curves: list[dict[str, Any]] = []
    for i, item in enumerate(_list(raw.get("curves"))[:4]):
        if not isinstance(item, dict):
            continue
        curve: dict[str, Any] = {"id": _id(item.get("id"), f"curve{i + 1}")}
        label = _text(item.get("label"), 40)
        if label:
            curve["label"] = label
        fn = _expr(item.get("fn"))
        if fn:
            curve["fn"] = fn
        else:
            points = _points(item.get("points"))
            if len(points) < 2:
                continue          # 既没表达式也没点：这条曲线丢掉，别让其余曲线陪葬
            curve["points"] = points
        curve["id"] = _unique(curve["id"], [c["id"] for c in curves])
        color = _color(item.get("color"))
        if color:
            curve["color"] = color
        curves.append(curve)

    axes = raw.get("axes") if isinstance(raw.get("axes"), dict) else {}
    x_axis = _axis(axes.get("x"), (-10.0, 10.0))
    y_axis = _axis(axes.get("y"), (-10.0, 10.0))
    if x_axis[0] >= x_axis[1] or y_axis[0] >= y_axis[1]:
        return None
    out: dict[str, Any] = {
        "axes": {
            "style": "cross" if str(axes.get("style") or "") == "cross" else "frame",
            "x": {"label": _text(axes.get("x", {}).get("label") if isinstance(axes.get("x"), dict) else "", 16) or "x",
                  "min": x_axis[0], "max": x_axis[1]},
            "y": {"label": _text(axes.get("y", {}).get("label") if isinstance(axes.get("y"), dict) else "", 16) or "y",
                  "min": y_axis[0], "max": y_axis[1]},
        },
        "curves": curves,
    }
    ids = [c["id"] for c in curves]

    markers = []
    for i, item in enumerate(_list(raw.get("markers"))[:6]):
        if not isinstance(item, dict):
            continue
        x, y = _num(item.get("x")), _num(item.get("y"))
        if x is None or y is None:
            continue
        marker = {"id": _id(item.get("id"), f"mark{i + 1}"), "x": x, "y": y}
        text = _text(item.get("text"), 24)
        if text:
            marker["text"] = text
        marker["id"] = _unique(marker["id"], ids)
        markers.append(marker)
        ids.append(marker["id"])
    if markers:
        out["markers"] = markers

    guides = []
    for i, item in enumerate(_list(raw.get("guides"))[:4]):
        if not isinstance(item, dict):
            continue
        at = _num(item.get("at"))
        if at is None:
            continue
        guide = {"id": _id(item.get("id"), f"guide{i + 1}"),
                 "type": "h" if str(item.get("type")) == "h" else "v", "at": at}
        label = _text(item.get("label"), 24)
        if label:
            guide["label"] = label
        guide["id"] = _unique(guide["id"], ids)
        guides.append(guide)
        ids.append(guide["id"])
    if guides:
        out["guides"] = guides

    regions = []
    for i, item in enumerate(_list(raw.get("regions"))[:4]):
        if not isinstance(item, dict):
            continue
        frm, to = _num(item.get("from")), _num(item.get("to"))
        if frm is None or to is None or frm == to:
            continue
        region = {"id": _id(item.get("id"), f"region{i + 1}"),
                  "from": min(frm, to), "to": max(frm, to)}
        label = _text(item.get("label"), 24)
        if label:
            region["label"] = label
        region["id"] = _unique(region["id"], ids)
        regions.append(region)
        ids.append(region["id"])
    if regions:
        out["regions"] = regions
    return out, ids


def _build_board(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    out: dict[str, Any] = {}
    ids: list[str] = []

    strokes = []
    for i, item in enumerate(_list(raw.get("strokes"))[:24]):
        if not isinstance(item, dict):
            continue
        points = [_clamp_point(p) for p in _points(item.get("points"))]
        if len(points) < 2:
            continue
        stroke: dict[str, Any] = {
            "id": _unique(_id(item.get("id"), f"line{i + 1}"), ids),
            "points": points,
        }
        if item.get("closed"):
            stroke["closed"] = True
        if item.get("dash"):
            stroke["dash"] = True
        if item.get("arrow"):
            stroke["arrow"] = True
        color = _color(item.get("color"))
        if color:
            stroke["color"] = color
        width = _num(item.get("width"))
        if width:
            stroke["width"] = max(2.0, min(14.0, width))
        strokes.append(stroke)
        ids.append(stroke["id"])
    if strokes:
        out["strokes"] = strokes

    circles = []
    for i, item in enumerate(_list(raw.get("circles"))[:12]):
        if not isinstance(item, dict):
            continue
        x, y = _num(item.get("x")), _num(item.get("y"))
        r = _num(item.get("r"))
        if x is None or y is None or not r:
            continue
        circle = {"id": _unique(_id(item.get("id"), f"dot{i + 1}"), ids),
                  "x": _clamp(x, 0, CANVAS_W), "y": _clamp(y, 0, CANVAS_H),
                  "r": max(3.0, min(240.0, r))}
        color = _color(item.get("color"))
        if color:
            circle["color"] = color
        circles.append(circle)
        ids.append(circle["id"])
    if circles:
        out["circles"] = circles

    arrows = []
    for i, item in enumerate(_list(raw.get("arrows"))[:8]):
        if not isinstance(item, dict):
            continue
        frm, to = _clamp_point(item.get("from")), _clamp_point(item.get("to"))
        if frm is None or to is None:
            continue
        arrow = {"id": _unique(_id(item.get("id"), f"arrow{i + 1}"), ids),
                 "from": frm, "to": to}
        color = _color(item.get("color"))
        if color:
            arrow["color"] = color
        arrows.append(arrow)
        ids.append(arrow["id"])
    if arrows:
        out["arrows"] = arrows

    notes = []
    for i, item in enumerate(_list(raw.get("notes"))[:16]):
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 80)
        x, y = _num(item.get("x")), _num(item.get("y"))
        if not text or x is None or y is None:
            continue
        note = {"id": _unique(_id(item.get("id"), f"note{i + 1}"), ids),
                "x": _clamp(x, 0, CANVAS_W), "y": _clamp(y, 0, CANVAS_H), "text": text}
        size = _num(item.get("size"))
        if size:
            note["size"] = max(18.0, min(96.0, size))
        color = _color(item.get("color"))
        if color:
            note["color"] = color
        notes.append(note)
        ids.append(note["id"])
    if notes:
        out["notes"] = notes

    return (out, ids) if ids else None


def _build_flow(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    nodes, ids = [], []
    for i, item in enumerate(_list(raw.get("nodes"))[:9]):
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 48)
        if not text:
            continue
        node = {"id": _unique(_id(item.get("id"), f"n{i + 1}"), ids), "text": text}
        shape = str(item.get("shape") or "").strip().lower()
        if shape in ("box", "diamond", "pill", "note"):
            node["shape"] = shape
        nodes.append(node)
        ids.append(node["id"])
    if not nodes:
        return None
    id_set = set(ids)
    edges = []
    for item in _list(raw.get("edges"))[:12]:
        if not isinstance(item, dict):
            continue
        src, dst = str(item.get("from") or ""), str(item.get("to") or "")
        if src in id_set and dst in id_set and src != dst:
            edge = {"from": src, "to": dst}
            label = _text(item.get("label"), 24)
            if label:
                edge["label"] = label
            if str(item.get("style") or "") == "dashed":
                edge["style"] = "dashed"
            edges.append(edge)
    out: dict[str, Any] = {"nodes": nodes}
    if edges:
        out["edges"] = edges
    return out, ids


def _build_concept(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    center = raw.get("center") if isinstance(raw.get("center"), dict) else {}
    center_text = _text(center.get("text"), 40)
    if not center_text:
        return None
    out: dict[str, Any] = {"center": {"id": _id(center.get("id"), "center"),
                                      "text": center_text}}
    ids = [out["center"]["id"]]
    branches = []
    for i, item in enumerate(_list(raw.get("branches"))[:6]):
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 40)
        if not text:
            continue
        branch = {"id": _unique(_id(item.get("id"), f"b{i + 1}"), ids), "text": text}
        children = []
        for child in _list(item.get("children"))[:4]:
            if isinstance(child, dict):
                child_text = _text(child.get("text"), 60)
                if child_text:
                    children.append({"text": child_text})
            elif isinstance(child, str) and child.strip():
                children.append({"text": _text(child, 60)})
        if children:
            branch["children"] = children
        branches.append(branch)
        ids.append(branch["id"])
    if not branches:
        return None
    out["branches"] = branches
    return out, ids


def _build_compare(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    ids: list[str] = []
    out: dict[str, Any] = {}
    for side, prefix in (("left", "l"), ("right", "r")):
        block = raw.get(side) if isinstance(raw.get(side), dict) else {}
        title = _text(block.get("title"), 24)
        items = []
        for i, item in enumerate(_list(block.get("items"))[:6]):
            if isinstance(item, str):
                text = _text(item, 40)
                raw_id = ""
            elif isinstance(item, dict):
                text = _text(item.get("text"), 40)
                raw_id = item.get("id")
            else:
                continue
            if not text:
                continue
            item_id = _unique(_id(raw_id, f"{prefix}{i + 1}"), ids)
            items.append({"id": item_id, "text": text})
            ids.append(item_id)
        if not title or not items:
            return None
        # 容器 id 走引擎默认值（left / right）：先亮标题，再逐条亮要点
        out[side] = {"title": title, "items": items}
        ids.append(side)
    conclusion = raw.get("conclusion") if isinstance(raw.get("conclusion"), dict) else {}
    conclusion_text = _text(conclusion.get("text"), 40)
    if conclusion_text:
        conclusion_id = _unique(_id(conclusion.get("id"), "conclusion"), ids)
        out["conclusion"] = {"id": conclusion_id, "text": conclusion_text}
        ids.append(conclusion_id)
    # 顺序：左栏 → 右栏 → 结论（各栏自己的条目在 steps 里插进来才符合讲解节奏）
    return out, [x for x in ids]


def _build_layers(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    layers, ids = [], []
    for i, item in enumerate(_list(raw.get("layers"))[:7]):
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), 24)
        if not label:
            continue
        layer = {"id": _unique(_id(item.get("id"), f"l{i + 1}"), ids), "label": label}
        detail = _text(item.get("detail"), 40)
        if detail:
            layer["detail"] = detail
        ratio = _num(item.get("ratio"))
        if ratio and ratio > 0:
            layer["ratio"] = max(0.3, min(4.0, ratio))
        color = _color(item.get("color"))
        if color:
            layer["color"] = color
        layers.append(layer)
        ids.append(layer["id"])
    return ({"layers": layers}, ids) if layers else None


def _build_timeline(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    events, ids = [], []
    for i, item in enumerate(_list(raw.get("events"))[:6]):
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 40)
        if not text:
            continue
        event = {"id": _unique(_id(item.get("id"), f"e{i + 1}"), ids),
                 "date": _text(item.get("date"), 16), "text": text}
        detail = _text(item.get("detail"), 40)
        if detail:
            event["detail"] = detail
        events.append(event)
        ids.append(event["id"])
    return ({"events": events}, ids) if events else None


def _build_cycle(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
    nodes, ids = [], []
    for i, item in enumerate(_list(raw.get("nodes"))[:6]):
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 32)
        if not text:
            continue
        node = {"id": _unique(_id(item.get("id"), f"c{i + 1}"), ids), "text": text}
        nodes.append(node)
        ids.append(node["id"])
    if len(nodes) < 2:
        return None
    out: dict[str, Any] = {"nodes": nodes}
    center_text = _text(raw.get("center_text"), 40)
    if center_text:
        out["center_text"] = center_text
    return out, ids


_BUILDERS = {
    "curve": _build_curve,
    "board": _build_board,
    "flow": _build_flow,
    "concept": _build_concept,
    "compare": _build_compare,
    "layers": _build_layers,
    "timeline": _build_timeline,
    "cycle": _build_cycle,
}


# ==================================================================== 小工具

def _clean_question(text: str) -> str:
    """题干归一：压空白、去控制字符。缓存键就是它，所以必须稳定。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:1200]


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None  # 挡 NaN/inf


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _clamp_point(point: Any) -> list[float] | None:
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    x, y = _num(point[0]), _num(point[1])
    if x is None or y is None:
        return None
    return [_clamp(x, 0, CANVAS_W), _clamp(y, 0, CANVAS_H)]


def _points(value: Any) -> list[list[float]]:
    return [p for p in (_clamp_point(item) for item in _list(value)) if p]


def _axis(value: Any, default: tuple[float, float]) -> tuple[float, float]:
    axis = value if isinstance(value, dict) else {}
    low = _num(axis.get("min"))
    high = _num(axis.get("max"))
    low = default[0] if low is None else low
    high = default[1] if high is None else high
    if low > high:
        low, high = high, low
    return low, high


def _color(value: Any) -> str:
    token = str(value or "").strip()
    if token in ("a1", "a2", "a3", "a4", "accent"):
        return token
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", token):
        return token
    return ""


def _expr(value: Any) -> str:
    """校验函数表达式：只放行「数字 / x / pi / e / 四则运算 / 白名单函数」。"""
    src = re.sub(r"\s+", "", str(value or ""))
    if not src or len(src) > 80 or not _EXPR_CHARS.fullmatch(src):
        return ""
    for word in _WORD.findall(src):
        if word not in _EXPR_TOKENS:
            return ""
    return src


def _id(value: Any, fallback: str) -> str:
    """模型给的 id 合法就用，否则（缺失 / 中文 / 超长 / 带空格）换成确定性的占位 id。"""
    token = str(value or "").strip()
    return token if _ID_OK.fullmatch(token) else fallback


def _unique(candidate: str, used: list[str]) -> str:
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}{index}" in used:
        index += 1
    return f"{candidate}{index}"


def subject_allowed(subject: str) -> bool:
    """本学科要不要画图：白名单见 ``settings.diagram_subjects``（空 = 全学科）。"""
    allowed = [s.strip() for s in str(settings.diagram_subjects or "").split(",") if s.strip()]
    if not allowed:
        return True
    return any(word in str(subject or "") for word in allowed)


@lru_cache(maxsize=1)
def get_diagram_builder() -> DiagramSpecBuilder:
    """进程级默认规格生成器（与答疑共用同一个 LLM 客户端单例）。"""
    return DiagramSpecBuilder()
