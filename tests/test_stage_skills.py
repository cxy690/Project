"""学段技能（小学 / 初中）专项测试。

钉住的行为：

1. ``.agents/skills/<学科>/stages/<stage>.md`` 每份都能被解析且字段完整；
2. 学段技能 != 学科默认技能（人格文本、skill_id、depth 都不同）；
3. 学段由 ``scratch["grade"]`` 推断，识别不出时回退默认技能；
4. 只给「该学段真的开课」的学科写技能（小学没有物理/化学/生物/历史/政治）；
5. 学科默认技能（11 个）与人格类数量不因新增学段而变 —— 向后兼容。
"""
from __future__ import annotations

import pytest

from config.stages import (
    JUNIOR,
    PRIMARY,
    STAGE_NAMES,
    STAGES,
    normalize_stage,
    stage_from_grade,
    stages_for_subject,
)
from config.subjects import SUBJECT_BY_KEY, SUBJECTS
from config.teacher_skills.md import (
    iter_stage_docs,
    load_doc,
    parse_skill_md,
    skill_path,
)
from core.skills.teacher_personality import (
    SKILL_ORDER,
    SKILL_STAGE_ORDER,
    STAGE_SKILLS,
    SKILLS,
)
from core.skills.teacher_personality.base import TeacherPersonalitySkill

#: 小学确实开设的学科（按 config/subjects.py 的 grades 字段）
PRIMARY_KEYS = {key for key in SUBJECT_BY_KEY if "小学" in SUBJECT_BY_KEY[key]["grades"]}
#: 初中确实开设的学科
JUNIOR_KEYS = {key for key in SUBJECT_BY_KEY if "初中" in SUBJECT_BY_KEY[key]["grades"]}


# ---------------------------------------------------------------- 学段枚举
def test_stage_enums_are_stable() -> None:
    assert STAGES == (PRIMARY, JUNIOR)
    assert STAGE_NAMES == {PRIMARY: "小学", JUNIOR: "初中"}
    assert normalize_stage("小学") == PRIMARY
    assert normalize_stage("junior") == JUNIOR
    assert normalize_stage("") == ""
    assert normalize_stage("高中") == ""


@pytest.mark.parametrize(
    ("grade", "expected"),
    [
        ("一年级", PRIMARY),
        ("小学三年级", PRIMARY),
        ("小五", PRIMARY),
        ("六年级", PRIMARY),
        ("初一", JUNIOR),
        ("初二", JUNIOR),
        ("初中二年级", JUNIOR),
        ("九年级", JUNIOR),
        ("", ""),
        ("高一", ""),
        ("随便写的", ""),
    ],
)
def test_stage_from_grade(grade: str, expected: str) -> None:
    assert stage_from_grade(grade) == expected


def test_stages_for_subject_follows_curriculum() -> None:
    assert stages_for_subject("math") == (PRIMARY, JUNIOR)
    assert stages_for_subject("science") == (PRIMARY,)
    assert stages_for_subject("physics") == (JUNIOR,)
    assert stages_for_subject("history") == (JUNIOR,)
    assert stages_for_subject("不存在") == ()


# ---------------------------------------------------------------- 文件与解析
def test_every_stage_file_parses_and_is_complete() -> None:
    docs = iter_stage_docs()
    assert docs, "至少要有小学/初中的学段技能"
    for key, stage, path in docs:
        front, sections = parse_skill_md(path.read_text(encoding="utf-8"))
        assert front.get("subject_key") == key, f"{path} 的 subject_key 与目录不一致"
        assert front.get("stage") == stage, f"{path} 的 stage 与文件名不一致"
        for title in (
            "说话风格", "讲课方式", "人格与教学", "开场方式", "追问方式",
            "鼓励方式", "表扬方式", "纠错方式", "学生说「我不会」时",
            "学生连续答错时", "白板组织", "语气与节奏", "进场问候",
        ):
            assert sections.get(title), f"{path} 缺小节：{title}"
        doc = load_doc(key, stage)
        assert doc is not None and doc.is_complete()


def test_stage_files_only_for_subjects_that_open_that_stage() -> None:
    for key, stage, path in iter_stage_docs():
        assert stage in stages_for_subject(key), (
            f"{path}：{key} 在课程方案里没有「{STAGE_NAMES[stage]}」学段，不应写这份技能"
        )


def test_primary_and_junior_are_different_skills() -> None:
    """同一学科的小学/初中必须是两份不同内容，而不是复制粘贴。"""
    for key in PRIMARY_KEYS & JUNIOR_KEYS:
        primary = load_doc(key, PRIMARY)
        junior = load_doc(key, JUNIOR)
        if primary is None or junior is None or primary.stage != PRIMARY or junior.stage != JUNIOR:
            continue
        assert primary.skill_id != junior.skill_id
        assert primary.system_prompt() != junior.system_prompt()
        assert primary.section("讲课方式") != junior.section("讲课方式")
        assert primary.depth != junior.depth


def test_stage_skill_path_layout() -> None:
    path = skill_path("math", PRIMARY)
    assert path.parent.name == "stages" and path.name == "primary.md"
    assert skill_path("math").name == "SKILL.md"


def test_missing_stage_falls_back_to_default() -> None:
    """没写学段版的学科回退默认技能（doc.stage 为空）。"""
    doc = load_doc("physics", PRIMARY)          # 物理只写了 junior
    assert doc is not None
    assert doc.stage == ""
    assert doc.path.name == "SKILL.md"


# ---------------------------------------------------------------- 注册表
def test_default_skill_counts_unchanged() -> None:
    """新增学段不改变「一科一人格」的默认注册结果（向后兼容）。"""
    assert len(SKILL_ORDER) == len(SKILLS) == len(SUBJECTS) == 11
    assert all(not cls.stage for cls in SKILL_ORDER)


def test_stage_skill_classes_registered() -> None:
    assert len(SKILL_STAGE_ORDER) == len(STAGE_SKILLS) == len(iter_stage_docs())
    for cls in SKILL_STAGE_ORDER:
        assert issubclass(cls, TeacherPersonalitySkill)
        assert cls.stage in (PRIMARY, JUNIOR)
        assert cls.skill_id.startswith(f"teacher.{cls.subject_id}.")


def test_registry_lookup_by_stage() -> None:
    from core.skills.teacher_personality import get_skill_registry

    registry = get_skill_registry()
    primary = registry.get_for_subject("math", PRIMARY)
    junior = registry.get_for_subject("math", JUNIOR)
    default = registry.get_for_subject("math")
    assert primary is not None and primary.skill_id.endswith("shuxue")
    assert junior is not None and junior.skill_id.endswith("tuili")
    assert default is not None and default.skill_id.endswith("yanli")
    assert primary.stage == PRIMARY and junior.stage == JUNIOR


def test_registry_falls_back_to_default_for_unknown_stage() -> None:
    from core.skills.teacher_personality import get_skill_registry

    registry = get_skill_registry()
    fallback = registry.get_for_subject("physics", PRIMARY)
    assert fallback is not None and fallback.stage == ""


def test_get_for_session_uses_grade() -> None:
    from core.skills.teacher_personality import get_skill_registry

    class _Session:
        def __init__(self, grade: str, subject_id: str) -> None:
            self.scratch = {"grade": grade, "subject_id": subject_id}

    registry = get_skill_registry()
    primary = registry.get_for_session(_Session("三年级", "math"))
    junior = registry.get_for_session(_Session("初二", "math"))
    unknown = registry.get_for_session(_Session("", "math"))
    assert primary is not None and primary.stage == PRIMARY
    assert junior is not None and junior.stage == JUNIOR
    assert unknown is not None and unknown.stage == ""
