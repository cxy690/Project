"""微信助手：家长日报的绑定、生成、发送与定时调度。

边界：
* 只读取学情报告（``core.learning``）与监护关系，不改课堂链路；
* 默认 ``WECHAT_PROVIDER=mock``，只写本地 outbox，不调用任何外部接口；
* 真正发送走企业微信群机器人 / Server酱等**官方或合规通道**，不接入个人微信自动化。
"""
from core.wechat.bindings import (
    add_binding,
    delete_binding,
    ensure_guardian_link,
    list_bindings,
    list_options,
)
from core.wechat.report import build_daily_report, reports_for_date
from core.wechat.service import (
    list_deliveries,
    preview_daily_report,
    send_all_today,
    send_daily_report,
    status_summary,
)

__all__ = [
    "add_binding",
    "build_daily_report",
    "delete_binding",
    "ensure_guardian_link",
    "list_bindings",
    "list_deliveries",
    "list_options",
    "preview_daily_report",
    "reports_for_date",
    "send_all_today",
    "send_daily_report",
    "status_summary",
]
