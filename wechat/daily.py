"""每日学情推送（默认 22:00）：当天有课才发，周日把本周周报并进同一条。

为什么单独一层而不是复用课后推送：

* 课后推送是「一节课一条」，家长一天上两节就收两条，且时间不可控；
* 这里是「一天一条」，只在当天**有课**时发，22:00 统一收口；
* 周日再加一段本周汇总，**同一条消息**发出 —— 企业微信群机器人一天连发两条
  会被当成刷屏，而且免费公众号还有「每天 1 次」的硬限制。

分群口径（与 :mod:`wechat.groups` 保持一致，别在文案层另开一套）：

========================  ==========================================  ==========================
群性质                     今日/本周内容                                 是否出现姓名与分数
========================  ==========================================  ==========================
``staff`` 内部群（测试/教师）  每个学生一行明细                                  可以
``parent`` 一对一家校群       只有该生明细                                      可以
``class`` 班级群（多家长）     只出人数、节数、平均分                             不可以
========================  ==========================================  ==========================

幂等：``daily:{群}:{日期}``；周日合并时额外占用 ``weekly:{群}:{周一日期}``，
与 :func:`wechat.notifier.push_weekly_all` 共用同一把锁，手动补发不会重复。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from config.logging import get_logger
from wechat import groups as groups_mod
from wechat.client import get_client
from wechat.config import load_config
from wechat.notifier import pushed_before, remember_pushed, week_range

log = get_logger("wechat.daily")

_WEEKDAY_CN = "一二三四五六日"
#: 明细最多列多少个学生（再多就刷屏，且 markdown 有 4096 字节上限）
MAX_DETAIL_STUDENTS = 10
#: 周报里最多列多少个学生
MAX_WEEKLY_STUDENTS = 6


# ------------------------------------------------------------------ 小工具
def _avg(items: Iterable[Any], attr: str) -> float:
    values = [float(getattr(item, attr, 0) or 0) for item in items]
    return sum(values) / len(values) if values else 0.0


def _score(value: float) -> str:
    """分数保留一位小数、去掉无意义的小数位：88.0 → 88，91.67 → 91.7。"""
    return f"{round(float(value), 1):g}"


def _pct(value: float) -> str:
    return f"{float(value) * 100:.0f}%"


def _day_bounds(now: datetime) -> tuple[float, float]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now.timestamp()


def _day_label(now: datetime) -> str:
    return f"{now.month}月{now.day}日（周{_WEEKDAY_CN[now.weekday()]}）"


def _by_student(items: Iterable[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for metrics in items:
        grouped.setdefault(str(metrics.student_id), []).append(metrics)
    return grouped


def _name_of(student_id: str, group: groups_mod.Group) -> str:
    return groups_mod.student_label(student_id, group) or f"学生{student_id[-4:]}"


# ------------------------------------------------------------------ 文案
def _student_block(student_id: str, items: list[Any], group: groups_mod.Group) -> list[str]:
    """一名学生当天的几行明细（掌握度取当天平均）。"""
    name = _name_of(student_id, group)
    subject = items[0].subject or "综合"
    lines = [
        f"**{name}** · {len(items)} 节 · {subject}",
        f"· 掌握 {_score(_avg(items, 'mastery_score'))} · "
        f"完课 {_score(_avg(items, 'completion_score'))} · "
        f"互动 {_pct(_avg(items, 'interaction_rate'))}",
    ]
    quiz_total = sum(int(getattr(item, "quiz_total", 0) or 0) for item in items)
    if quiz_total:
        quiz_first = sum(int(getattr(item, "quiz_first_correct", 0) or 0) for item in items)
        lines.append(f"· 练习 {quiz_total} 题 · 首答对 {quiz_first} 题")
    flags: list[str] = []
    for item in items:
        for flag in list(getattr(item, "flags", []) or [])[:1]:
            if flag not in flags:
                flags.append(flag)
    if flags:
        # 只说第一条：群里一行太长会挤掉后面的学生，明细在后台能看全
        lines.append(f"· 待关注：{flags[0][:28]}")
    return lines


def _aggregate_lines(items: list[Any], by_student: dict[str, list[Any]]) -> list[str]:
    """班级群口径：只有人数 / 节数 / 平均分，不点名、不出现个人分。"""
    minutes = sum(int(getattr(item, "effective_seconds", 0) or 0) for item in items) // 60
    return [
        f"今日 {len(by_student)} 名学生上课 · 共 {len(items)} 节 · {minutes} 分钟",
        f"平均掌握 {_score(_avg(items, 'mastery_score'))} · "
        f"平均完课 {_score(_avg(items, 'completion_score'))} · "
        f"平均互动 {_pct(_avg(items, 'interaction_rate'))}",
    ]


def _weekly_block(
    items: list[Any],
    label: str,
    group: groups_mod.Group,
    *,
    detail: bool,
) -> list[str]:
    """本周汇总（周一 ~ 今天）。``detail`` 为真时才按学生列分。"""
    if not items:
        return []
    by_student = _by_student(items)
    lines = [
        f"**本周周报{label}** · 共 {len(items)} 节 · {len(by_student)} 人",
        f"· 平均掌握 {_score(_avg(items, 'mastery_score'))} · "
        f"平均完课 {_score(_avg(items, 'completion_score'))}",
    ]
    if not detail:
        return lines
    ordered = sorted(by_student.items(), key=lambda kv: -len(kv[1]))
    for student_id, lessons in ordered[:MAX_WEEKLY_STUDENTS]:
        lines.append(
            f"· {_name_of(student_id, group)} {len(lessons)} 节 · "
            f"掌握 {_score(_avg(lessons, 'mastery_score'))}"
        )
    if len(ordered) > MAX_WEEKLY_STUDENTS:
        lines.append(f"· …另有 {len(ordered) - MAX_WEEKLY_STUDENTS} 人，明细见后台")
    return lines


def build_daily_text(
    group: groups_mod.Group,
    items: list[Any],
    *,
    now: datetime,
    weekly_items: list[Any] | None = None,
    week_label: str = "",
    base_url: str = "",
) -> str:
    """拼一条企业微信群 markdown（今日 + 可选本周）。"""
    by_student = _by_student(items)
    detail = group.allows_detail
    lines: list[str] = [f"### 今日学情 · {_day_label(now)}"]

    if group.kind == groups_mod.KIND_PARENT and len(by_student) == 1:
        student_id = next(iter(by_student))
        lines.append(f"**{_name_of(student_id, group)}**")
        lines.extend(_student_block(student_id, by_student[student_id], group)[1:])
    elif detail:
        lines.extend(_aggregate_lines(items, by_student))
        lines.append("")
        ordered = sorted(by_student.items(), key=lambda kv: kv[0])
        for student_id, lessons in ordered[:MAX_DETAIL_STUDENTS]:
            lines.extend(_student_block(student_id, lessons, group))
    else:
        lines.extend(_aggregate_lines(items, by_student))
        lines.append("")
        lines.append("（按隐私口径，班级群不展示单个学生的分数，明细请登录后台查看）")

    if weekly_items:
        block = _weekly_block(weekly_items, week_label, group, detail=detail)
        if block:
            lines.append("")
            lines.append("---")
            lines.extend(block)

    if base_url:
        lines.append("")
        lines.append(f"[查看完整学情报告]({base_url}/app/learning_report/)")

    return "\n".join(lines)


# ------------------------------------------------------------------ 推送
async def push_daily_all(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    force: bool = False,
    include_weekly: bool | None = None,
    group_id: str = "",
) -> list[dict[str, Any]]:
    """把所有群当天的学情推一遍，返回每群结果（脚本直接打印）。

    * ``dry_run`` / ``force`` 可绕过 ``WECHAT_NOTIFY_DAILY`` 与去重（预览用）；
    * ``now`` 可指定任意时刻（补发 / 测试）；
    * 周日默认合并本周周报，但要 ``WECHAT_NOTIFY_WEEKLY=true`` 才真的带上。
    """
    cfg = load_config()
    if not cfg.notify_daily and not (dry_run or force):
        log.info("WECHAT_NOTIFY_DAILY 未开启，跳过每日推送")
        return []

    now = now or datetime.now()
    is_sunday = now.weekday() == 6
    include_weekly = is_sunday if include_weekly is None else include_weekly
    if include_weekly and not cfg.notify_weekly and not (dry_run or force):
        log.warning("今天是周日，但 WECHAT_NOTIFY_WEEKLY 未开启，本次只发日报")
        include_weekly = False

    from core.learning_stats import list_lessons  # noqa: PLC0415 - 避免启动期循环依赖

    day_start, day_end = _day_bounds(now)
    lessons = list_lessons(limit=5000)
    today_items = [m for m in lessons if day_start <= float(m.started_at) <= day_end]
    if not today_items:
        log.info("今日无上课记录，跳过每日推送")
        return [{"skipped": "今日无上课记录"}]

    by_student = _by_student(today_items)
    week_since, _, _ = week_range(now.timestamp())
    week_label = f"（{datetime.fromtimestamp(week_since):%m-%d} ~ {now:%m-%d}）"
    week_tag = datetime.fromtimestamp(week_since).strftime("%Y-%m-%d")
    weekly_items = (
        [m for m in lessons if week_since <= float(m.started_at) <= day_end]
        if include_weekly else []
    )

    results: list[dict[str, Any]] = []
    for group in groups_mod.load_groups():
        if group_id and group.id != group_id:
            continue
        if not group.daily:
            results.append({"group": group.id, "name": group.name, "skipped": "该群未开启日报"})
            continue

        targets = list(group.student_ids) or (
            list(by_student.keys()) if group.allows_detail or group.is_class else []
        )
        if not targets:
            results.append({"group": group.id, "name": group.name, "skipped": "群内无学生"})
            continue
        items = [m for sid in targets for m in by_student.get(sid, [])]
        if not items:
            results.append({"group": group.id, "name": group.name, "skipped": "群内学生今日无课"})
            continue

        day_key = f"daily:{group.id}:{now:%Y-%m-%d}"
        if not (dry_run or force) and pushed_before(day_key):
            results.append({"group": group.id, "name": group.name, "skipped": "今日已推送"})
            continue

        group_week_items = (
            [m for m in weekly_items if str(m.student_id) in set(targets)]
            if weekly_items else []
        )
        if include_weekly and not group_week_items:
            results.append({"group": group.id, "name": group.name, "skipped": "本周无上课记录"})
            continue

        text = build_daily_text(
            group,
            items,
            now=now,
            weekly_items=group_week_items,
            week_label=week_label,
            base_url=cfg.report_base_url,
        )

        if dry_run:
            results.append({
                "group": group.id, "name": group.name, "kind": group.kind,
                "sent": True, "dry_run": True, "lessons": len(items), "text": text,
            })
            continue

        ok = await get_client().send_markdown(text, webhook=group.webhook)
        results.append({
            "group": group.id, "name": group.name, "kind": group.kind,
            "sent": ok, "lessons": len(items),
            "weekly": bool(group_week_items), "error": "" if ok else "发送失败",
        })
        if ok:
            remember_pushed(day_key)
            if group_week_items:
                remember_pushed(f"weekly:{group.id}:{week_tag}")
            extra = "，含本周周报" if group_week_items else ""
            log.info("每日学情已推送：群 %s（%s 节%s）", group.name or group.id, len(items), extra)

    return results


__all__ = ["MAX_DETAIL_STUDENTS", "MAX_WEEKLY_STUDENTS", "build_daily_text", "push_daily_all"]
