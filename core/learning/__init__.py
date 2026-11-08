"""学情分析：课堂事件采集、指标计算与报告查询。

对外入口：
* :func:`core.learning.events.record_event` 等采集函数（运行时旁路调用）
* :func:`core.learning.service.get_report` / ``list_reports``（API 调用）
* :func:`core.learning.metrics.compute_report`（纯计算，测试友好）
"""
from core.learning.events import (
    finish_session,
    observe_session_event,
    record_course_start,
    record_event,
    record_heartbeat,
    record_session_start,
    record_student_turn,
)
from core.learning.metrics import compute_report, summarize_reports
from core.learning.service import (
    get_report,
    get_session_detail,
    list_reports,
    summarize,
)

__all__ = [
    "compute_report",
    "finish_session",
    "get_report",
    "get_session_detail",
    "list_reports",
    "observe_session_event",
    "record_course_start",
    "record_event",
    "record_heartbeat",
    "record_session_start",
    "record_student_turn",
    "summarize",
    "summarize_reports",
]
