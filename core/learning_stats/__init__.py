"""学情分析平台：采集 → 指标 → 监控 → 报告。

对外只用这几个入口::

    from core.learning_stats import get_collector, compute_metrics, build_report

    collector = get_collector()
    collector.student_message(state, "老师，这道题怎么做")   # 学生发言
    metrics = collector.finish(state)                      # 下课时结算
    report = build_report(metrics)                         # 课后报告

指标口径见 :mod:`core.learning_stats.metrics`；数据落在 ``data/learning/``，
**不写云端、不依赖 MySQL**（课堂事件是高频小写入，走文件更稳）。
"""
from core.learning_stats.collector import (
    LearningCollector,
    get_collector,
    observe_session_event,
)
from core.learning_stats.metrics import (
    IDLE_GAP_SEC,
    compute_metrics,
    effective_time,
    mastery_level_of,
    summarize_lessons,
)
from core.learning_stats.models import (
    EventKind,
    LessonEvent,
    LessonMetrics,
    LessonReport,
    MasteryLevel,
)
from core.learning_stats.report import build_report, headline_of
from core.learning_stats.store import (
    list_lessons,
    list_students,
    read_events,
    read_metrics,
    read_student_index,
)

__all__ = [
    "IDLE_GAP_SEC",
    "EventKind",
    "LearningCollector",
    "LessonEvent",
    "LessonMetrics",
    "LessonReport",
    "MasteryLevel",
    "build_report",
    "compute_metrics",
    "effective_time",
    "get_collector",
    "headline_of",
    "list_lessons",
    "list_students",
    "mastery_level_of",
    "observe_session_event",
    "read_events",
    "read_metrics",
    "read_student_index",
    "summarize_lessons",
]
