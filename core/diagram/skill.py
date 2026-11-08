"""运行期技能的加载器：``.agents/agent_skills/<name>/SKILL.md`` 是事实源。

为什么要有这一层
----------------
教学图的生成指令以前硬编码在 :mod:`core.diagram.tikz` 的 ``_PROMPT`` 里，
而同一套规则在 ``tikz-teaching`` 技能包中也存在一份 —— 两份必然漂移，
改了一边另一边还是旧行为，且改 prompt 必须重启服务。

现在规则只在技能包里写一次：
``SKILL.md`` 的「出图规划提示词」段落 + ``templates/*.tex`` + ``scripts/check_tikz_safety.py``，
运行期按 mtime 重载，改 md 即生效。

与 ``.agents/skills/`` 的区别
----------------------------
``.agents/skills/<学科>/SKILL.md`` 是**教师人格话术**（``config/teacher_skills`` 消费，
frontmatter 要求 ``subject_key/persona/tone``… 十个字段）。
本目录 ``.agents/agent_skills/`` 是**可执行技能**（Agent Skill 格式：
``name/description`` + 可被运行期执行的 ``scripts/``）。两者格式不兼容，放错目录会被
人格解析器判为坏技能并回退默认 persona，所以必须分开。
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from config.logging import get_logger

log = get_logger("core.diagram.skill")

#: 项目根（core/diagram/skill.py -> parents[2]）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 可执行技能根目录。环境变量便于测试与换盘部署。
SKILLS_ROOT = Path(
    os.environ.get("DIAGRAM_SKILLS_DIR") or (PROJECT_ROOT / ".agents" / "agent_skills")
)

#: 负责运行期出图的技能名。
#: 注意与 ``.agents/tools/digital-teacher-diagram`` 区分：那份是离线 Pillow 渲染器
#: （产 PNG/SVG 交付物，人工跑），本技能是运行期 TikZ 出图（讲课时自动触发）。
SKILL_NAME = "tikz-teacher-diagram"

#: SKILL.md 里承载生成指令的小节标题（按 `##` 标题匹配）
PROMPT_SECTION = "出图规划提示词"

#: 题干占位符。用 replace 而不是 str.format —— 提示词里全是 LaTeX 花括号
QUESTION_SLOT = "@@QUESTION@@"

#: 模板里的正文插入点
BODY_SLOT = "@@BODY@@"

#: (路径 -> (mtime, 内容))：改文件即失效，不用重启
_text_cache: dict[Path, tuple[float, str]] = {}
#: (路径 -> (mtime, 模块))：脚本改动后重新加载，避免拿着旧代码跑
_module_cache: dict[Path, tuple[float, ModuleType]] = {}


def skill_dir(name: str = SKILL_NAME) -> Path:
    return SKILLS_ROOT / name


def _read(path: Path) -> str | None:
    """读文本，按 mtime 缓存；文件不存在返回 ``None``。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _text_cache.pop(path, None)
        return None
    hit = _text_cache.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:  # noqa: BLE001 - 技能文件读不到不该炸掉课堂
        log.warning("技能文件读取失败 {}：{}", path, err)
        _text_cache.pop(path, None)
        return None
    _text_cache[path] = (mtime, text)
    return text


def _extract_block(markdown: str, heading_keyword: str) -> str:
    """取某个 ``##`` 小节里第一个围栏代码块的内容。

    围栏可以是 ``` 或 ````，取到与之配对的收尾行为止；小节里没有代码块返回空串。
    """
    lines = markdown.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and heading_keyword in stripped:
            start = index + 1
            break
    if start is None:
        return ""

    fence: str | None = None
    buf: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if fence is None:
            if stripped.startswith("```"):
                fence = stripped[: len(stripped) - len(stripped.lstrip("`"))] or "```"
                continue
            # 已经遇到同级的下一个标题还没找到代码块，说明这节没有
            if stripped.startswith("## "):
                return ""
        else:
            if stripped.startswith(fence):
                return "\n".join(buf).strip()
            buf.append(line)
    return ""


def plan_prompt() -> str | None:
    """SKILL.md 里的生成指令原文（仍带 ``@@QUESTION@@`` 占位）。"""
    markdown = _read(skill_dir() / "SKILL.md")
    if markdown is None:
        return None
    block = _extract_block(markdown, PROMPT_SECTION)
    if not block:
        log.warning("技能 {} 的「{}」小节里没有代码块", SKILL_NAME, PROMPT_SECTION)
        return None
    return block


def render_prompt(question: str, template: str) -> str:
    """把题干填进技能提示词。"""
    return template.replace(QUESTION_SLOT, question)


def preamble() -> str | None:
    """导言区模板（``@@BODY@@`` 处插入每步源码）。

    占位串必须唯一：模板注释里若手滑写了同样的串，``str.replace`` 会把正文插进注释，
    编译出来是一张空图 —— 所以这里把多余的那些就地替换掉，只保留最后一个作为插入点。
    """
    text = _read(skill_dir() / "templates" / "slide_16x9_ctex.tex")
    if text is None:
        return None
    count = text.count(BODY_SLOT)
    if count == 0:
        log.warning("模板缺少 {} 占位，忽略该模板", BODY_SLOT)
        return None
    if count > 1:
        head, _, tail = text.rpartition(BODY_SLOT)
        text = head.replace(BODY_SLOT, "（插入点）") + BODY_SLOT + tail
        log.warning("模板里 {} 出现了 {} 次，只保留最后一处作为插入点", BODY_SLOT, count)
    return text


def load_script(filename: str) -> ModuleType | None:
    """动态加载技能内的脚本（目录名带连字符/点，无法常规 import）。"""
    path = skill_dir() / "scripts" / filename
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _module_cache.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        spec = importlib.util.spec_from_file_location(
            f"diagram_skill_{path.stem}", path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as err:  # noqa: BLE001 - 脚本坏了退回"不检查"，不能停课
        log.warning("技能脚本加载失败 {}：{}", path, err)
        return None
    _module_cache[path] = (mtime, module)
    return module


def safety_checker() -> Callable[..., list[str]] | None:
    """``check_source(text, fragment=True)``，技能缺脚本时返回 ``None``。"""
    module = load_script("check_tikz_safety.py")
    checker = getattr(module, "check_source", None) if module else None
    return checker if callable(checker) else None


def engine_hint() -> str | None:
    """SKILL.md 里声明的 TeX 引擎提示（正文里的 ``引擎：xxx`` 行），仅供配置兜底。"""
    text = _read(skill_dir() / "SKILL.md")
    if text is None:
        return None
    match = re.search(r"^引擎：\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def snapshot() -> dict[str, Any]:
    """当前技能加载状态，给 ``/health`` 与排障用。"""
    root = skill_dir()
    prompt = plan_prompt()
    return {
        "root": str(root),
        "exists": root.exists(),
        "skill_md": (root / "SKILL.md").exists(),
        "template": (root / "templates" / "slide_16x9_ctex.tex").exists(),
        "safety_script": (root / "scripts" / "check_tikz_safety.py").exists(),
        "prompt_chars": len(prompt or ""),
    }
