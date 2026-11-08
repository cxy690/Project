"""教师技能 Markdown 事实源：读取 ``.agents/skills/<学科>/SKILL.md``。

为什么是 md 而不是 Python（一次合并）：
之前同一份「这位老师怎么说话、怎么讲课」被写了两遍 ——
``config/teacher_skills/<key>.py``（风格/讲法/深度/语速）与
``core/skills/teacher_personality/<key>_teacher.py``（人格与九项教学行为），
内容重叠、改一处漏一处。现在**一份 md 承载两者**：

* **frontmatter** 承载结构化字段（身份、深度、语速、讲课风格、口头禅、务必做到/不要做）；
* **正文小节** 承载成段文本（说话风格、讲课方式、人格与教学、九项行为、进场问候）。

本模块只负责「读 + 解析」，不认识任何 Python 具体类型：
上层各自把 :class:`SkillDoc` 适配成自己的对象（``TeacherSkill`` / ``TeacherPersonalitySkill``）。

目录约定::

    .agents/skills/<学科key>/SKILL.md            # 该学科默认技能（运行期事实源）
    .agents/skills/<学科key>/stages/primary.md   # 小学学段专属技能（覆盖默认）
    .agents/skills/<学科key>/stages/junior.md    # 初中学段专属技能（覆盖默认）
    .agents/skills/<学科key>/references/         # 流派设计稿（人读，不注入提示词）

``<学科key>`` 与 ``data/teachers/<key>/``、``config/subjects.py`` 的 ``key`` 一致。

**学段覆盖规则**（见 :func:`load_doc`）：给了 ``stage`` 且对应
``stages/<stage>.md`` 存在时用它，否则回退学科默认 ``SKILL.md``。
于是「没写学段版」的学科行为与拆分前完全一致，属于纯增量。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config.logging import get_logger

log = get_logger("config.teacher_skills.md")

#: 项目根（config/teacher_skills/md.py -> parents[2]）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: 技能根目录：``<项目根>/.agents/skills``（可用环境变量覆盖，便于测试/换盘）
SKILLS_DIR = PROJECT_ROOT / ".agents" / "skills"
#: 每个学科目录下的文件名
SKILL_FILENAME = "SKILL.md"
#: 学段专属技能的子目录（``<学科>/stages/<stage>.md``）
STAGES_SUBDIR = "stages"

#: 正文小节标题 -> 行为键（标题就是 md 里人读的 ``## xxx``）
BEHAVIOR_SECTIONS: dict[str, str] = {
    "开场方式": "opening_style",
    "追问方式": "questioning_style",
    "鼓励方式": "encouragement_style",
    "表扬方式": "praise_style",
    "纠错方式": "correction_style",
    "学生说「我不会」时": "confused_student_strategy",
    "学生连续答错时": "repeated_error_strategy",
    "白板组织": "whiteboard_style",
    "语气与节奏": "tone_rhythm",
}

#: 正文小节标题 -> 风格层字段（``TeacherSkill``）
STYLE_SECTIONS: dict[str, str] = {
    "说话风格": "style_persona",
    "讲课方式": "teaching_way",
}

#: 正文小节标题 -> 人格层整段提示词
PROMPT_SECTION = "人格与教学"
#: 正文小节标题 -> 进场问候模板
GREETING_SECTION = "进场问候"

#: frontmatter 必填字段（缺一个就认为这份技能不完整，调用方回退默认 persona）
REQUIRED_FRONT: tuple[str, ...] = (
    "subject_key",
    "subject_name",
    "skill_id",
    "display_name",
    "persona",
    "tone",
    "depth",
    "speech_rate",
    "teaching_style",
    "style_persona",
)


def _strip_notes(lines: list[str]) -> str:
    """去掉「给人看的注解」（``>`` 引用块与 HTML 注释），只留要注入提示词的正文。"""
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">") or stripped.startswith("<!--"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def parse_skill_md(text: str) -> tuple[dict[str, Any], dict[str, str]]:
    """把一份 SKILL.md 拆成 ``(frontmatter, {小节标题: 正文})``。

    小节按 ``## 标题`` 切分；标题之外的零散段落（文档导语）会被忽略 ——
    导语是给人读的，不参与提示词装配。
    """
    if not text.startswith("---"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter")
    _, raw_front, body = text.split("---", 2)
    front = yaml.safe_load(raw_front) or {}
    if not isinstance(front, dict):
        raise ValueError("SKILL.md 的 frontmatter 必须是键值表")

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return front, {title: _strip_notes(lines) for title, lines in sections.items()}


@dataclass(frozen=True)
class SkillDoc:
    """一位老师的 SKILL.md（解析结果）：身份元信息 + 成段文本。"""

    key: str                    # 学科 key（= 目录名）
    path: Path                  # SKILL.md 路径（排障用）
    front: dict[str, Any]
    sections: dict[str, str]
    stage: str = ""             # 学段（primary/junior）；空 = 学科默认技能

    # ------------------------------------------------------------ 元信息
    def text(self, field: str, default: str = "") -> str:
        """取 frontmatter 里的字符串字段。"""
        value = self.front.get(field)
        return str(value).strip() if value is not None else default

    def items(self, field: str) -> tuple[str, ...]:
        """取 frontmatter 里的字符串列表字段（缺省为空元组）。"""
        raw = self.front.get(field) or []
        if isinstance(raw, str):
            raw = [raw]
        return tuple(str(x).strip() for x in raw if str(x).strip())

    @property
    def subject_key(self) -> str:
        return self.text("subject_key", self.key)

    @property
    def subject_name(self) -> str:
        return self.text("subject_name")

    @property
    def skill_id(self) -> str:
        return self.text("skill_id")

    @property
    def class_name(self) -> str:
        """该科人格 Skill 的 Python 类名（保持 ``from core.skills... import MathTeacherSkill`` 可用）。"""
        return self.text("class_name")

    @property
    def display_name(self) -> str:
        return self.text("display_name")

    @property
    def persona(self) -> str:
        """一句话人格关键词（日志 / 前端展示用）。"""
        return self.text("persona")

    @property
    def tone(self) -> str:
        return self.text("tone")

    @property
    def depth(self) -> str:
        return self.text("depth", "standard")

    @property
    def speech_rate(self) -> float:
        try:
            return float(self.front.get("speech_rate", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def teaching_style(self) -> str:
        return self.text("teaching_style", "plain")

    # ------------------------------------------------------------ 成段文本
    def style_field(self, name: str, default: str = "") -> str:
        """风格层字段：优先 frontmatter（多行块），其次同名小节。"""
        value = self.front.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
        for title, field in STYLE_SECTIONS.items():
            if field == name:
                return self.section(title, default)
        return default

    def section(self, title: str, default: str = "") -> str:
        return self.sections.get(title, "") or default

    def behavior(self, name: str, default: str = "") -> str:
        """按行为键取正文小节（键见 :data:`BEHAVIOR_SECTIONS`）。"""
        for title, field in BEHAVIOR_SECTIONS.items():
            if field == name:
                return self.section(title, default)
        return default

    def system_prompt(self) -> str:
        return self.section(PROMPT_SECTION)

    def greeting(self, where: str = "") -> str:
        """进场问候：``{where}`` 替换为当前课题，留空用 ``greeting_default``。"""
        template = self.section(GREETING_SECTION)
        if not template:
            return ""
        target = where or self.text("greeting_default", "今天的课程")
        return template.replace("{where}", target)

    def is_complete(self) -> bool:
        """必填字段是否齐全（不齐时调用方应回退默认 persona，而不是注入半份人格）。"""
        return all(self.text(f) for f in REQUIRED_FRONT)


# ---------------------------------------------------------------- 加载
def skill_path(key: str, stage: str = "") -> Path:
    """技能文件路径：有学段版取 ``stages/<stage>.md``，否则学科默认 ``SKILL.md``。

    注意这里**不做存在性判断** —— 判断在 :func:`load_doc` 里做（回退默认技能）。
    """
    base = SKILLS_DIR / str(key or "").strip()
    stage = str(stage or "").strip()
    if stage:
        return base / STAGES_SUBDIR / f"{stage}.md"
    return base / SKILL_FILENAME


#: 解析缓存：``"<key>|<stage>" -> (mtime, doc|None)``。按 **修改时间** 失效，
#: 所以线上改一份 SKILL.md 后下一次调用即生效，不必重启进程。
_cache: dict[str, tuple[float, "SkillDoc | None"]] = {}


def _read_doc(path: Path, key: str, stage: str) -> SkillDoc | None:
    """读并解析一份技能文件；失败返回 ``None``（只告警，不抛）。"""
    try:
        front, sections = parse_skill_md(path.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001 - 一份技能格式坏掉不该拖垮整节课
        log.warning("SKILL.md 解析失败，已跳过：{}（{}）", path, err)
        return None
    doc = SkillDoc(key=key, path=path, front=front, sections=sections, stage=stage)
    if not doc.is_complete():
        log.warning("SKILL.md 必填字段缺失，已跳过：{}", path)
        return None
    return doc


def load_doc(key: str, stage: str = "") -> SkillDoc | None:
    """按学科 key（+ 可选学段）加载技能；不存在 / 解析失败返回 ``None``。

    回退顺序：
    1. ``<key>/stages/<stage>.md``（该学段专属技能，存在且字段完整即用）；
    2. ``<key>/SKILL.md``（学科默认技能，``doc.stage == ""``）。

    这样「只给部分学科写了学段版」也能工作：没写的学科行为与拆分前一致。
    """
    key = str(key or "").strip()
    if not key:
        return None
    stage = str(stage or "").strip()
    cache_key = f"{key}|{stage}"
    path = skill_path(key, stage)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if not stage:
            _cache.pop(cache_key, None)
            return None
        # 学段版缺失 -> 回退学科默认技能（不缓存成学段结果，避免改文件后不生效）
        return load_doc(key, "")
    cached = _cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    doc = _read_doc(path, key, stage)
    if doc is None and stage:
        # 学段版写坏了：宁可回退到学科默认技能，也不要整科人格消失
        log.warning("学段技能不可用，回退学科默认：{}", path)
        return load_doc(key, "")
    _cache[cache_key] = (mtime, doc)
    return doc


def available_keys() -> tuple[str, ...]:
    """已定义技能的学科 key 列表（按目录名排序）。"""
    if not SKILLS_DIR.is_dir():
        return ()
    return tuple(sorted(p.parent.name for p in SKILLS_DIR.glob(f"*/{SKILL_FILENAME}")))


def available_stages(key: str) -> tuple[str, ...]:
    """某学科已写好的学段技能（``stages/*.md`` 的文件名，按字母序）。"""
    folder = SKILLS_DIR / str(key or "").strip() / STAGES_SUBDIR
    if not folder.is_dir():
        return ()
    return tuple(sorted(p.stem for p in folder.glob("*.md")))


def iter_stage_docs() -> tuple[tuple[str, str, Path], ...]:
    """``[(学科key, 学段, 路径)]``：遍历所有已存在的学段技能文件。"""
    if not SKILLS_DIR.is_dir():
        return ()
    return tuple(
        (path.parent.parent.name, path.stem, path)
        for path in sorted(SKILLS_DIR.glob(f"*/{STAGES_SUBDIR}/*.md"))
    )


def reload_docs() -> None:
    """清空解析缓存（测试 / 强制重读用）。"""
    _cache.clear()
