"""教师技能（Skill）加载器：读 ``.agents/skills/<学科key>/SKILL.md``。

事实源只有一份 md（见 :mod:`config.teacher_skills.md`），本包负责把它适配成
风格层对象 :class:`TeacherSkill`，供 ``agents/shared/teaching_layers.py`` 拼进提示词。

用法::

    from config.teacher_skills import load_skill

    skill = load_skill("chinese")   # -> TeacherSkill | None
    skill.prompt_text()             # 注入 LLM 的「说话风格 + 讲课方式 + 深度 + 口头禅」
"""
from __future__ import annotations

from config.teacher_skills.base import DEPTH_GUIDE, TeacherSkill
from config.teacher_skills.md import (
    BEHAVIOR_SECTIONS,
    SKILLS_DIR,
    STYLE_SECTIONS,
    SkillDoc,
    available_keys,
    available_stages,
    iter_stage_docs,
    load_doc,
    parse_skill_md,
    reload_docs,
    skill_path,
)

__all__ = [
    "BEHAVIOR_SECTIONS",
    "DEPTH_GUIDE",
    "SKILLS_DIR",
    "STYLE_SECTIONS",
    "SkillDoc",
    "TeacherSkill",
    "available_keys",
    "available_skills",
    "available_stages",
    "iter_stage_docs",
    "load_doc",
    "load_skill",
    "parse_skill_md",
    "reload_docs",
    "skill_path",
]


def load_skill(key: str, stage: str = "") -> TeacherSkill | None:
    """按学科 key（+ 可选学段）加载技能；md 不可用时返回 ``None``（调用方回退）。

    ``stage`` 为空时行为与拆分前完全一致（读 ``SKILL.md``）。
    """
    doc = load_doc(key, stage)
    return TeacherSkill.from_doc(doc) if doc is not None else None


def available_skills() -> list[str]:
    """已定义技能的学科 key 列表（= ``.agents/skills/`` 下的目录名）。"""
    return list(available_keys())
