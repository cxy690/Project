"""学情分析的数据结构：事件、指标、报告。

**为什么事件和指标分开**：
事件（:class:`LessonEvent`）是"课堂上真实发生过什么"的原始记录，只追加、不改写；
指标（:class:`LessonMetrics`）是从事件**算出来**的结论，随时可以重算。
把两者分开，才能做到「算法改了、历史数据还能按新口径重算」，也才能审计
——家长质疑「为什么说孩子互动率低」时，能翻出每一次发言与提问的时间点。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """课堂事件类型（写入 jsonl 的 ``kind`` 字段）。"""

    SESSION_START = "session_start"       # 开课
    SESSION_END = "session_end"           # 下课
    STUDENT_TEXT = "student_text"         # 学生发言（提问 / 作答 / 闲聊）
    TEACHER_QUESTION = "teacher_question" # 老师提问（追问、出题）
    QUIZ_ASKED = "quiz_asked"             # 出了一道题
    QUIZ_ANSWERED = "quiz_answered"       # 学生对某道题作答（含对错）
    PHASE = "phase"                       # 进入课程阶段（讲解 / 互动出题）
    IDLE = "idle"                         # 客户端上报的挂机 / 长时间无操作
    INTERRUPT = "interrupt"               # 学生打断老师（主动参与信号）


class MasteryLevel(str, Enum):
    """掌握度分级（阈值见 :data:`MASTERY_THRESHOLDS`）。"""

    NOT_REACHED = "未达标"
    INITIAL = "初识"
    UNDERSTAND = "理解"
    APPLY = "应用"
    PROFICIENT = "熟练"


#: 掌握度分档下界（含），从高到低匹配
MASTERY_THRESHOLDS: tuple[tuple[float, MasteryLevel], ...] = (
    (95.0, MasteryLevel.PROFICIENT),
    (85.0, MasteryLevel.APPLY),
    (75.0, MasteryLevel.UNDERSTAND),
    (60.0, MasteryLevel.INITIAL),
    (0.0, MasteryLevel.NOT_REACHED),
)


@dataclass
class LessonEvent:
    """一条课堂事件（jsonl 的一行）。

    ``data`` 只放该事件专属的字段（如作答的 ``correct`` / ``latency``），
    不放会话级信息 —— 那些在文件头或指标里，避免每行重复。
    """

    ts: float
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ts": round(float(self.ts), 3), "kind": self.kind, "data": self.data}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LessonEvent":
        return cls(
            ts=float(raw.get("ts") or 0.0),
            kind=str(raw.get("kind") or ""),
            data=dict(raw.get("data") or {}),
        )


@dataclass
class LessonMetrics:
    """一节课的学情指标（0—100 分制，字段含义见 ``core/learning_stats/metrics.py``）。"""

    lesson_id: str
    student_id: str
    subject: str = "综合"
    stage: str = ""
    grade: str = ""

    started_at: float = 0.0
    ended_at: float = 0.0
    planned_seconds: int = 0           # 计划课时（默认 40 分钟）
    wall_seconds: int = 0              # 实际挂机时长（含离场）
    effective_seconds: int = 0         # 有效学习时长（剔除离场空档）
    idle_seconds: int = 0              # 离场总时长
    idle_count: int = 0                # 离场次数

    phases_planned: int = 2            # 有计划时长的阶段数（讲解 / 互动出题）
    phases_done: int = 0               # 实际进入过的阶段数
    wrap_done: bool = False            # 是否完成收尾检测（后 25% 时段有作答）

    teacher_questions: int = 0         # 老师提问次数
    student_turns: int = 0             # 学生发言轮次
    student_initiations: int = 0       # 学生主动发起（不是回答）
    interrupts: int = 0                # 打断次数
    response_median_sec: float = 0.0   # 学生对提问的响应中位时延

    quiz_total: int = 0
    quiz_correct: int = 0
    quiz_first_correct: int = 0        # 首次作答即正确的题数

    completion_score: float = 0.0      # 完课度 0—100
    interaction_rate: float = 0.0      # 互动率 = 学生发言 / 老师提问（0—1）
    initiative_rate: float = 0.0       # 主动率 = 主动发起 / 学生发言（0—1）
    accuracy: float = 0.0              # 正确率（含订正）
    first_accuracy: float = 0.0        # 首答正确率
    mastery_score: float = 0.0         # 掌握度 0—100
    mastery_level: str = ""            # 掌握度分级
    authenticity_score: float = 100.0 # 学习真实性 0—100
    overall_score: float = 0.0         # 综合评分 0—100

    flags: list[str] = field(default_factory=list)   # 风险 / 异常提示（给老师看）
    computed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LessonMetrics":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (raw or {}).items() if k in known}
        clean["flags"] = list(clean.get("flags") or [])
        return cls(**clean)


@dataclass
class LessonReport:
    """给老师 / 家长看的课后结论（由 :mod:`core.learning_stats.report` 生成）。"""

    lesson_id: str
    student_id: str
    headline: str                       # 一句话结论
    highlights: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    parent_note: str = ""               # 家长版摘要
    metrics: LessonMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["metrics"] = self.metrics.to_dict() if self.metrics else None
        return data
