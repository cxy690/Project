"""学情查询服务：事件 -> 报告、最近会话列表、单节课详情。

API 层只依赖这里，不直接读文件；这样以后把事件换成 MySQL / ClickHouse 时
不需要改路由与前端。
"""
from __future__ import annotations

from typing import Any

from collections import defaultdict

from config.settings import settings
from core.learning.metrics import REPORT_VERSION, compute_report, summarize_reports
from core.learning.store import load_events, load_report, save_report


def get_report(
    session_id: str,
    *,
    days: int | None = None,
    use_cache: bool = True,
) -> dict[str, Any] | None:
    """按 session_id 生成报告；有缓存且事件未更新时直接用缓存。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    events = load_events(session_id=sid, days=days)
    if not events:
        return load_report(sid)
    if use_cache:
        cached = load_report(sid)
        if (
            cached
            and int(cached.get("schema_version") or 0) == REPORT_VERSION
            and int(cached.get("event_count") or 0) == len(events)
        ):
            return cached
    report = compute_report(events, sid)
    save_report(report)
    return report


def list_reports(
    *,
    student_id: str = "",
    days: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """最近会话报告（按最近事件倒序）。

    一次读入窗口内的事件再按 session 分组，避免 N 个会话把文件读 N 遍；
    这也是学情看板 30 秒轮询下的主要性能保障。
    """
    cap = max(1, min(int(limit or 50), 200))
    window = days if days is not None else int(settings.learning_retention_days)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in load_events(days=window, student_id=student_id):
        sid = str(event.get("session_id") or "").strip()
        if sid:
            grouped[sid].append(event)

    reports: list[dict[str, Any]] = []
    for sid, events in grouped.items():
        report = compute_report(events, sid)
        cached = load_report(sid)
        if (
            not cached
            or int(cached.get("schema_version") or 0) != REPORT_VERSION
            or int(cached.get("event_count") or 0) != len(events)
        ):
            save_report(report)
        reports.append(report)
    reports.sort(key=lambda item: float(item.get("started_at") or 0.0), reverse=True)
    return reports[:cap]


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_reports(reports)


def get_session_detail(
    session_id: str,
    *,
    days: int | None = None,
    event_limit: int = 300,
) -> dict[str, Any] | None:
    """报告 + 事件时间线（心跳只保留前后各一条，避免看板被 15 秒心跳刷屏）。"""
    report = get_report(session_id, days=days)
    if report is None:
        return None
    events = load_events(session_id=session_id, days=days)
    heartbeats = [item for item in events if item.get("event_type") == "heartbeat"]
    timeline = [item for item in events if item.get("event_type") != "heartbeat"]
    if heartbeats:
        timeline.append(heartbeats[0])
        if len(heartbeats) > 1:
            timeline.append(heartbeats[-1])
    timeline.sort(key=lambda item: float(item.get("ts") or 0.0))
    return {
        "report": report,
        "events": timeline[-max(1, int(event_limit)):],
        "heartbeat_count": len(heartbeats),
    }
