"""讲解配图规划：把「这道题怎么讲」翻译成一份前端能直接渲染的教学图规格。

定位：只服务答疑讲解。老师讲题时，白板右侧/上方配一张会「一步步亮起来」的图，
讲到哪一步图就跟着走到哪一步。

三条硬规矩（配图是锦上添花，绝不能拖累讲课）：

1. **失败即放弃**。任何异常、JSON 不合法、字段不合规，一律返回 ``None``，
   调用方直接跳过配图，讲课流程不受任何影响。
2. **白名单校验**。模型输出不可信：图型只认 5 种结构最简单的（曲线要表达式采样，
   错误率高，暂不开放）、节点数 3~6、每一步必须引用真实存在的节点 id。
3. **不写 LaTeX**。前端引擎不做公式排版，式子用 Unicode 表达（a² + b² = c²）。
"""
from __future__ import annotations

import re
from typing import Any

from config.logging import get_logger
from models.llm import get_llm

log = get_logger("agents.shared.diagram_planner")

#: 允许的图型。curve 需要函数表达式采样，模型写错率高，暂不开放。
ALLOWED_TYPES = ("flow", "concept", "compare", "layers", "cycle")
MAX_NODES = 6
MAX_TEXT = 16

_SYSTEM = """你是教学配图设计师。你要根据一道题的讲解过程，判断「配一张什么样的图最能帮学生听懂」，并输出图的规格。

第一步，判断该不该配图：
- 纯计算、纯记忆、纯语言类（字词解释、造句、朗读、单词拼写）→ 不配图，输出 {"skip": true}
- 涉及**步骤流程、概念拆解、两者对比、纵向层级、循环**→ 配图

第二步，选图型（只能选一个）：
- flow：有先后顺序的做法/步骤（如解方程步骤、实验流程）
- concept：把一个概念拆成几个构成要素（如「力的三要素」）
- compare：两个对象逐项对照（如「有丝分裂 vs 减数分裂」）
- layers：纵向层级/圈层结构（如「地球内部圈层」）
- cycle：首尾相接的循环（如水循环、碳循环）

第三步，输出 JSON：

{"skip": false,
 "type": "flow",
 "title": "不超过 12 字",
 "nodes": [{"id": "n1", "text": "节点文字", "shape": "box"}],
 "edges": [{"from": "n1", "to": "n2", "label": "不超过 6 字"}],
 "steps": [{"reveal": ["n1"], "narration": "讲到这一步时老师说的一句话"}]}

硬性约束，违反即作废：
- nodes 只能 3~6 个；每个节点的 text **不超过 12 个字**，是板书短语，不是整句话，结尾不加句号
- steps 数量必须等于 nodes 数量，且第 i 个 step 只点亮第 i 个节点（reveal 里只放一个 id）
- edges 的 from/to 必须是已存在的 node id；flow 类必须有 edges，concept/layers 可以没有
- shape 只能取 box / pill / note
- **绝对不要使用 LaTeX、不要用 $ 符号、不要用 \\frac**；数学式子写成 a² + b² = c² 这种 Unicode 形式
- 只输出 JSON，不要解释"""

_USER_TMPL = """学科：{subject}　学段：{grade}
题目/问题：{question}
最终答案：{answer}
老师的讲解标题：{title}
老师的讲解步骤：
{steps}

请判断该不该配图，并按要求输出 JSON。"""


def _clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    """板书短语：去空白、去重点星号、去模型爱漏的 LaTeX 残留（$ 和 \\frac 之类）。"""
    text = re.sub(r"\s+", "", str(value or ""))
    text = text.replace("**", "").replace("$", "")
    text = re.sub(r"\\[a-zA-Z]+\{?", "", text)      # \frac{ → 去掉命令名
    text = text.replace("}", "").replace("{", "")
    return text.strip("，。、；;,.!！?？ ")[:limit]


def _validate(raw: Any) -> dict[str, Any] | None:
    """把模型输出裁剪成引擎能吃的规格；任何不合规都返回 None。"""
    if not isinstance(raw, dict) or raw.get("skip"):
        return None

    dtype = str(raw.get("type") or "").strip()
    if dtype not in ALLOWED_TYPES:
        log.debug("diagram: 图型 {!r} 不在白名单，丢弃", dtype)
        return None

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or not (3 <= len(nodes_raw) <= MAX_NODES):
        log.debug("diagram: nodes 数量不合规，丢弃")
        return None

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(nodes_raw, 1):
        if not isinstance(item, dict):
            return None
        nid = str(item.get("id") or f"n{i}").strip()
        text = _clean_text(item.get("text"))
        if not nid or nid in seen or not text:
            return None
        shape = str(item.get("shape") or "box").strip()
        nodes.append({
            "id": nid,
            "text": text,
            "shape": shape if shape in ("box", "pill", "note") else "box",
        })
        seen.add(nid)

    ids = {n["id"] for n in nodes}
    edges: list[dict[str, str]] = []
    for item in raw.get("edges") or []:
        if not isinstance(item, dict):
            continue
        src, dst = str(item.get("from") or ""), str(item.get("to") or "")
        if src in ids and dst in ids and src != dst:
            edge = {"from": src, "to": dst}
            label = _clean_text(item.get("label"), 8)
            if label:
                edge["label"] = label
            edges.append(edge)

    # steps 必须与 nodes 一一对应：第 i 步点亮第 i 个节点
    steps_raw = raw.get("steps")
    steps: list[dict[str, Any]] = []
    if isinstance(steps_raw, list) and len(steps_raw) == len(nodes):
        for i, (item, node) in enumerate(zip(steps_raw, nodes)):
            narration = ""
            if isinstance(item, dict):
                narration = str(item.get("narration") or "").strip()[:60]
            steps.append({"id": f"s{i + 1}", "reveal": [node["id"]], "narration": narration})
    else:
        steps = [
            {"id": f"s{i + 1}", "reveal": [n["id"]], "narration": ""}
            for i, n in enumerate(nodes)
        ]

    title = _clean_text(raw.get("title") or "", 14) or "看图理解"
    spec: dict[str, Any] = {
        "title": title,
        "type": dtype,
        "canvas": "portrait",     # 配图区在白板右半区，是竖长的，用竖屏画布才填得满
        "theme": "light",
        "safe_area": "none",
        "nodes": nodes,
        "edges": edges,
        "steps": steps,
    }
    return spec


async def plan_diagram(
    *,
    subject: str,
    grade: str,
    question: str,
    steps: list[str],
    title: str = "",
    answer: str = "",
) -> dict[str, Any] | None:
    """生成一份教学图规格；不值得配图或生成失败时返回 None。"""
    step_lines = "\n".join(f"{i}. {str(s)}" for i, s in enumerate(steps, 1) if str(s).strip())
    if not step_lines.strip():
        return None

    user = _USER_TMPL.format(
        subject=subject or "综合",
        grade=grade or "初中",
        question=(question or "").strip()[:300],
        answer=(answer or "").strip()[:60],
        title=(title or "").strip()[:40],
        steps=step_lines[:800],
    )

    try:
        raw = await get_llm().chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=700,
        )
    except Exception as err:  # noqa: BLE001 - 配图失败绝不影响讲课
        log.warning("diagram: 生成失败，跳过配图：{}", err)
        return None

    spec = _validate(raw)
    if spec is None:
        log.debug("diagram: 规格不合规，跳过配图")
    return spec
