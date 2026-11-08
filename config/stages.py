"""学段（小学 / 初中）元数据与推断。

为什么单独一个模块：
学段此前只是 ``config/subjects.py`` 里一段给人看的 ``grades`` 文本（如
``"小学 · 初中"``）加上会话里一个自由填写的 ``scratch["grade"]``。教师技能、
出题、提示词各自去猜「这个学生是小学还是初中」，口径不一。

现在把「学段」收敛成两个稳定枚举值：

* ``primary`` —— 小学；
* ``junior``  —— 初中。

推断入口只有 :func:`stage_from_grade`（自由文本 -> 枚举）与
:func:`stages_for_subject`（学科 -> 可开学段），其余模块一律引用这里，
不再各自写一遍 ``if "小学" in grade``。
"""
from __future__ import annotations

import re

from config.subjects import SUBJECT_BY_KEY, SUBJECTS

#: 稳定的学段枚举（顺序即展示顺序）
PRIMARY = "primary"
JUNIOR = "junior"
STAGES: tuple[str, ...] = (PRIMARY, JUNIOR)

#: 枚举 -> 中文名
STAGE_NAMES: dict[str, str] = {
    PRIMARY: "小学",
    JUNIOR: "初中",
}

#: 中文名 -> 枚举（兼容外部直接传「小学」/「初中」）
STAGE_BY_NAME: dict[str, str] = {name: key for key, name in STAGE_NAMES.items()}

#: 初中优先判（"初中二年级"同时含"二年级"，必须先命中初中，否则会被判成小学）
_JUNIOR_PATTERN = re.compile(r"初[一二三]|初中|[七八九]年级")
#: 小学提示（不包含单字，避免"初一"里的"一"误伤 —— 初中规则在前，这里只兜小学）
_PRIMARY_PATTERN = re.compile(r"小学|小[一二三四五六]|[一二三四五六]年级")


def normalize_stage(value: str | None) -> str:
    """把 ``primary`` / ``小学`` / 空 归一化为枚举值；无法识别返回空串。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in STAGES:
        return raw
    return STAGE_BY_NAME.get(str(value).strip(), "")


def stage_from_grade(grade: str | None) -> str:
    """从自由文本年级推断学段（``"三年级"`` -> ``primary``）。

    识别失败返回空串 —— 调用方应回退到学科默认技能，而不是瞎猜一个学段：
    猜错学段比没有学段更糟（小学课件套初中话术会直接讲到学生听不懂）。
    """
    raw = str(grade or "").strip()
    if not raw:
        return ""
    direct = normalize_stage(raw)
    if direct:
        return direct
    # 顺序很重要："初中二年级"同时含"二年级"，初中必须优先命中
    if _JUNIOR_PATTERN.search(raw):
        return JUNIOR
    if _PRIMARY_PATTERN.search(raw):
        return PRIMARY
    return ""


def stages_for_subject(key: str) -> tuple[str, ...]:
    """某学科按现行课程方案可开的学段（解析 ``SUBJECTS`` 的 ``grades`` 文本）。

    小学没有物理/化学/生物/历史/政治，初中没有独立的「科学」——
    这些差异直接来自 ``config/subjects.py`` 的 ``grades`` 字段，不另维护一份表。
    """
    meta = SUBJECT_BY_KEY.get(str(key or "").strip())
    if not meta:
        return ()
    grades = str(meta.get("grades") or "")
    out: list[str] = []
    if "小学" in grades:
        out.append(PRIMARY)
    if "初中" in grades:
        out.append(JUNIOR)
    return tuple(out)


def stage_label(stage: str | None) -> str:
    """学段枚举 -> 中文名；未知返回原值（便于日志排障）。"""
    key = normalize_stage(stage)
    return STAGE_NAMES.get(key, str(stage or ""))


def all_stage_pairs() -> list[tuple[str, str]]:
    """``[(学科key, 学段)]`` 全集，供自检 / 管理接口遍历。"""
    return [
        (str(subject["key"]), stage)
        for subject in SUBJECTS
        for stage in stages_for_subject(str(subject["key"]))
    ]


# ---------------------------------------------------------------- 兼容别名
# 早期分支里这两个枚举叫 STAGE_PRIMARY / STAGE_JUNIOR、学科口径叫 subject_stages，
# 从另一台实例合并过来的管理页/路由用的是那套名字。值完全一致，这里给出别名，
# 避免同一概念两套常量各写一遍（写错了就是「小学加载初中技能」这种事故）。
STAGE_PRIMARY = PRIMARY
STAGE_JUNIOR = JUNIOR
STAGE_KEYS: tuple[str, ...] = STAGES
subject_stages = stages_for_subject


def subject_supports_stage(subject: str | None, stage: str | None) -> bool:
    """该学科是否覆盖指定学段；学科或学段未知时返回 True（不拦截）。"""
    st = normalize_stage(stage)
    covered = subject_stages(str(subject or ""))
    if not st or not covered:
        return True
    return st in covered
