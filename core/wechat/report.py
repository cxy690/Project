"""把某位学生某一天的学情报告汇总成一条家长可读的微信日报。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from config.settings import settings
from core.learning.metrics import STATUS_LABELS
from core.learning.service import list_reports

_FLAG_LABELS = {
    "no_quiz": "今天没有出题",
    "no_answer": "没有作答记录",
    "low_interaction": "互动偏少",
    "low_mastery": "掌握度偏低",
    "no_summary": "没有走到总结环节",
    "left_early": "提前离开",
    "no_heartbeat": "缺少在场心跳",
    "no_course_start": "没有正式开课",
    "safety_alert": "出现安全提醒",
}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.wechat_report_timezone or "Asia/Shanghai")
    except Exception:  # noqa: BLE001 - 无效时区回退
        return ZoneInfo("Asia/Shanghai")


def report_date_of(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), _tz()).date().isoformat()


def reports_for_date(student_id: str, date: str) -> list[dict[str, Any]]:
    """取该学生指定本地日期的已结束课堂报告。"""
    sid = (student_id or "").strip()
    day = (date or "").strip()
    if not sid or not day:
        return []
    reports = list_reports(student_id=sid, days=max(3, int(settings.learning_retention_days)), limit=200)
    rows = []
    for report in reports:
        when = (
            float(report.get("ended_at") or 0.0)
            or float(report.get("updated_at") or 0.0)
            or float(report.get("started_at") or 0.0)
        )
        if report_date_of(when) == day and report.get("status") != "in_progress":
            rows.append(report)
    rows.sort(key=lambda item: float(item.get("started_at") or 0.0))
    return rows


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{round(number * 100)}%"


def _avg(values: list[Any]) -> float | None:
    nums = []
    for value in values:
        try:
            nums.append(float(value))
        except (TypeError, ValueError):
            continue
    return sum(nums) / len(nums) if nums else None


def build_daily_report(
    *,
    student: dict[str, Any],
    guardian: dict[str, Any],
    date: str,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """返回 ``{title, text, report, date, student, guardian}``；不负责发送。"""
    student_name = str(
        student.get("display_name") or student.get("username") or "孩子"
    ).strip()
    guardian_name = str(
        guardian.get("display_name") or guardian.get("username") or "家长"
    ).strip()
    guardian_prefix = guardian_name
    total = len(reports)
    completed = sum(1 for item in reports if item.get("status") == "completed")
    active_seconds = sum(int(item.get("active_seconds") or 0) for item in reports)
    minutes = round(active_seconds / 60)
    completion = _avg([item.get("completion_rate") for item in reports])
    mastery = _avg([item.get("mastery") for item in reports])
    interaction = _avg([item.get("interaction_rate") for item in reports])
    flags = sorted({flag for item in reports for flag in (item.get("flags") or [])})
    status_counts: dict[str, int] = {}
    for item in reports:
        label = str(item.get("status_label") or STATUS_LABELS.get(item.get("status") or "", "—"))
        status_counts[label] = status_counts.get(label, 0) + 1

    subjects: dict[str, list[dict[str, Any]]] = {}
    for item in reports:
        subjects.setdefault(str(item.get("subject") or "综合"), []).append(item)

    if total and completed == total:
        overall = "完成学习"
    elif completed:
        overall = "部分完成"
    elif any(item.get("status") == "unverified" for item in reports):
        overall = "未验证"
    elif any(item.get("status") == "abandoned" for item in reports):
        overall = "中途离开"
    elif total:
        overall = "部分完成"
    else:
        overall = "今天没有课程记录"

    lines = [
        f"【{student_name}今日学习汇报】{date}",
        f"{guardian_prefix}您好，以下是{student_name}今天在 AI 课堂的学习情况：",
        "",
        f"今天共 {total} 节课，有效学习约 {minutes} 分钟。",
        f"整体状态：{overall}；完成学习 {completed} 节。",
    ]
    if total:
        lines.append(
            f"平均课时完成 {_pct(completion)}，"
            f"平均掌握 {_pct(mastery) if mastery is not None else '未验证'}，"
            f"平均互动 {_pct(interaction)}。"
        )
        lines.append("")
        lines.append("各科情况：")
        for subject, items in subjects.items():
            lines.append(
                f"· {subject}：{len(items)} 节，"
                f"完成 {_pct(_avg([x.get('completion_rate') for x in items]))}，"
                f"掌握 {_pct(_avg([x.get('mastery') for x in items]))}，"
                f"互动 {_pct(_avg([x.get('interaction_rate') for x in items]))}"
            )

    highlights = [
        f"{subject}已完成学习"
        for subject, items in subjects.items()
        if items and all(item.get("status") == "completed" for item in items)
    ]
    if mastery is not None and mastery >= 0.7:
        highlights.append(f"平均掌握度达到 {_pct(mastery)}")
    if interaction is not None and interaction >= 0.6:
        highlights.append(f"课堂互动保持 {_pct(interaction)}")
    if highlights:
        lines.extend(["", "今日亮点：" + "；".join(highlights[:3]) + "。"])

    concerns = [_FLAG_LABELS.get(flag, flag) for flag in flags if flag in _FLAG_LABELS]
    if mastery is not None and mastery < 0.5:
        concerns.append("掌握度偏低，建议睡前回顾今天的错题")
    if not total:
        concerns.append("今天没有完成任何课程")
    if concerns:
        lines.extend(["", "需要关注：" + "；".join(list(dict.fromkeys(concerns))[:4]) + "。"])

    base = (
        settings.wechat_public_base_url
        or f"http://127.0.0.1:{settings.port}"
    ).rstrip("/")
    if base and total:
        lines.extend([
            "",
            f"查看详情：{base}/app/learning_center/?student_id={quote(str(student.get('id') or ''))}",
        ])
    text = "\n".join(lines).strip()
    limit = max(200, int(settings.wechat_report_max_chars or 1800))
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"

    return {
        "title": f"{student_name}今日学习汇报 · {date}",
        "text": text,
        "date": date,
        "student": {
            "id": str(student.get("id") or ""),
            "name": student_name,
            "grade": student.get("grade") or "",
            "stage": student.get("stage") or "",
        },
        "guardian": {
            "id": str(guardian.get("id") or ""),
            "name": guardian_name,
        },
        "report": {
            "sessions": total,
            "completed": completed,
            "minutes": minutes,
            "completion_rate": round(completion, 3) if completion is not None else None,
            "mastery": round(mastery, 3) if mastery is not None else None,
            "interaction_rate": round(interaction, 3) if interaction is not None else None,
            "status": overall,
            "status_counts": status_counts,
            "subjects": {
                subject: {
                    "sessions": len(items),
                    "completion_rate": _avg([x.get("completion_rate") for x in items]),
                    "mastery": _avg([x.get("mastery") for x in items]),
                    "interaction_rate": _avg([x.get("interaction_rate") for x in items]),
                }
                for subject, items in subjects.items()
            },
            "flags": flags,
        },
    }
