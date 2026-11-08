"""学情通知推送（课后报告 / 周报 / 预警转人工）。

调用方式：

* 课堂内自动推送：``server/websocket.py`` 下课时调 :func:`schedule_lesson_report`
  （fire-and-forget，绝不等微信接口返回再关连接）；
* 运营手动补发：``scripts/push_weekly_reports.py``（支持 ``--dry-run`` 预览）。

推送对象按「群的性质」分流（见 :mod:`wechat.groups`）：

1. ``parent`` 一对一家校群：发该生本节明细 / 周报明细；
2. ``class`` 班级群：**只发「已生成，请私聊查看」或班级聚合概览**，
   任何单个学生的分数都不出现（群里其他家长能看到，属于未成年人数据外发）；
3. 没配 ``groups.json`` 时回退到 ``WECHAT_WEBHOOK_URL`` 的全局广播（旧行为，保持兼容）；
4. 家长私聊（微信客服 / 公众号）始终保留，作为明细的兜底通道。

防重：同一 ``lesson_id`` / 同一群同一周只推一次（``data/wechat/pushed.json``）。
断线重连会以新 session 重新结算，没有这层去重就会出现「一节课发两三条学情」。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.logging import get_logger
from wechat import groups as groups_mod
from wechat import handlers
from wechat.client import get_client
from wechat.config import WeChatConfig, load_config

log = get_logger("wechat.notifier")

#: 去重记录保留时长（30 天足够覆盖"补发上周周报"的场景）
PUSH_STATE_KEEP = 30 * 24 * 3600

#: 断线重连判定窗口：本次开课时间落在「上次结算时间 + 该秒数」内，视为同一节课。
#: 断线重连会以新 session 重新结算、生成新的 ``lesson_id``，只按 lesson_id 去重挡不住；
#: 取 300 秒是因为课间通常 ≥5 分钟 —— 真下课了会正常推，中途断线重连会被合并。
RECONNECT_WINDOW_SEC = 300


# ---------------------------------------------------------------------- 防重
def _state_path() -> Path:
    """推送去重记录（与 bindings.json 同目录，随环境隔离、不同步云端）。"""
    return handlers.BINDINGS_PATH.parent / "pushed.json"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 文件不存在/损坏都按空处理
        return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _already_pushed(key: str) -> bool:
    keys = _load_state().get("keys")
    return bool(key) and isinstance(keys, dict) and key in keys


def _last_window(student_id: str) -> dict[str, Any] | None:
    """该学生上一次已推送课次的时间窗（用于识别断线重连）。"""
    windows = _load_state().get("windows")
    if isinstance(windows, dict):
        item = windows.get(student_id)
        if isinstance(item, dict):
            return item
    return None


def _remember_window(student_id: str, started_at: float, ended_at: float) -> None:
    """记下本次课的时间窗，下次结算时用它判断是"同一节课重连"还是"新的一节课"。"""
    if not student_id or not started_at:
        return
    state = _load_state()
    windows = dict(state.get("windows") or {})
    windows[student_id] = {
        "started_at": float(started_at),
        "ended_at": float(ended_at or started_at),
        "at": time.time(),
    }
    cutoff = time.time() - PUSH_STATE_KEEP
    state["windows"] = {
        sid: item
        for sid, item in windows.items()
        if float((item or {}).get("at") or 0) >= cutoff
    }
    try:
        _save_state(state)
    except OSError as err:  # noqa: BLE001 - 记不上最多多推一次，不能影响课堂
        log.warning("课次时间窗写入失败：{}", err)


def _mark_pushed(key: str) -> None:
    if not key:
        return
    state = _load_state()
    keys = dict(state.get("keys") or {})
    now = time.time()
    keys[key] = now
    cutoff = now - PUSH_STATE_KEEP
    state["keys"] = {
        name: ts for name, ts in keys.items() if float(ts or 0) >= cutoff
    }
    try:
        _save_state(state)
    except OSError as err:  # noqa: BLE001 - 记不上最多重复发一次，不能影响课堂
        log.warning("推送去重记录写入失败：{}", err)


# ---------------------------------------------------------------------- 发送
async def _send_to_parents(
    student_id: str, text: str, cfg: WeChatConfig, *, dry_run: bool = False
) -> int:
    """给绑定了该学生的家长发消息，返回成功受理的条数。

    优先**微信客服**（家长扫码即可，不用关注公众号）；客服发送失败
    （多半是 48 小时窗口已过期）时回退到公众号 / 企微应用通道。
    两条都不通就返回 0，由群推送兜底。
    """
    if not student_id:
        return 0
    open_ids = handlers.open_ids_of(student_id)
    if not open_ids:
        log.info("学生 {} 没有绑定家长，跳过私聊推送", student_id)
        return 0
    if dry_run:
        print(f"--- [dry-run] 家长私聊 {student_id}（{len(open_ids)} 位）")
        print(text)
        return len(open_ids)

    if cfg.kf_enabled:
        from wechat.kf import get_kf_client  # noqa: PLC0415

        kf = get_kf_client()
        kfid = await kf.resolve_open_kfid()
        sent = 0
        for open_id in open_ids:
            if await kf.send_text(open_id, text, open_kfid=kfid):
                sent += 1
        if sent:
            return sent
        log.info("微信客服未送达 {}（可能超过 48 小时窗口），尝试其他通道", student_id)

    client = get_client()
    sent = 0
    for open_id in open_ids:
        if await client.send_text(open_id, text):
            sent += 1
    return sent


async def _send_group(group: groups_mod.Group, text: str, *, dry_run: bool = False) -> bool:
    """往一个群发文本（群机器人 webhook）。"""
    if dry_run:
        print(f"--- [dry-run] 群 {group.id} / {group.name}（{group.kind}）")
        print(text)
        return True
    ok = await get_client().send_group_text(text, webhook=group.webhook)
    if ok:
        log.info("学情已推送到群 {}（{}）", group.name or group.id, group.kind)
    else:
        log.warning("学情推送失败：群 {}（{}）", group.name or group.id, group.id)
    return ok


async def _send_legacy_webhook(text: str, cfg: WeChatConfig, *, dry_run: bool = False) -> bool:
    """旧行为兜底：``WECHAT_WEBHOOK_URL`` 全局广播（没配 groups.json 时才用）。"""
    if not cfg.webhook_enabled:
        return False
    if dry_run:
        print("--- [dry-run] 全局 webhook")
        print(text)
        return True
    return await get_client().send_group_text(text)


# ---------------------------------------------------------------------- 课后
async def push_lesson_report(metrics: Any, *, dry_run: bool = False) -> int:
    """推送一节课的学情，返回成功送达的条数。

    分流规则：
    * 一对一家校群 → 明细（``handlers.lesson_text``）；
    * 班级群 → 只有「已生成，请私聊查看」（``handlers.lesson_notice_text``）；
    * 家长私聊 → 明细（微信客服 / 公众号）。
    """
    cfg = load_config()
    if not cfg.notify_lesson_end:
        return 0
    lesson_id = str(getattr(metrics, "lesson_id", "") or "")
    dedup_key = f"lesson:{lesson_id}" if lesson_id else ""
    if dedup_key and _already_pushed(dedup_key):
        log.info("本节课学情已推送过，跳过（{}）", lesson_id)
        return 0
    effective = int(getattr(metrics, "effective_seconds", 0) or 0)
    if effective < int(cfg.min_lesson_seconds):
        log.info(
            "有效学习 {} 秒 < {} 秒，跳过课后推送（{}）",
            effective, cfg.min_lesson_seconds, lesson_id,
        )
        return 0

    student_id = str(getattr(metrics, "student_id", "") or "")
    started_at = float(getattr(metrics, "started_at", 0) or 0)
    ended_at = float(getattr(metrics, "ended_at", 0) or 0)

    # 断线重连：同一次课会以新 session 重新结算（新 lesson_id），lesson_id 去重挡不住。
    # 用「本次开课时间是否紧接上次结算」判断，重叠则视为同一节课，只保留第一次推送。
    previous = _last_window(student_id)
    if previous and started_at:
        if started_at <= float(previous.get("ended_at") or 0) + RECONNECT_WINDOW_SEC:
            log.info(
                "学生 {} 本次开课紧接上次结算，判定为断线重连，不重复推送（{}）",
                student_id, lesson_id,
            )
            return 0

    detail = handlers.lesson_text(metrics)
    sent = await _send_to_parents(student_id, detail, cfg, dry_run=dry_run)

    has_registry = groups_mod.GROUPS_PATH.is_file()
    for group in groups_mod.groups_for_student(student_id, for_lesson=True):
        if group.is_class:
            label = groups_mod.student_label(student_id, group)
            text = handlers.lesson_notice_text(
                metrics, label=label, show_name=group.show_student_name
            )
        else:
            text = detail
        if await _send_group(group, text, dry_run=dry_run):
            sent += 1

    # 没有群注册表时才用全局 webhook：否则班级群会收到明细，等于把分数广播出去
    if not has_registry:
        legacy = f"【课后学情】学生 {student_id}\n{detail}"
        if await _send_legacy_webhook(legacy, cfg, dry_run=dry_run):
            sent += 1

    if dedup_key and sent and not dry_run:
        _mark_pushed(dedup_key)
        _remember_window(student_id, started_at, ended_at)
    return sent


# ---------------------------------------------------------------------- 周报
def week_range(week_start: float | None = None) -> tuple[float, float, str]:
    """自然周区间（周一 00:00 起），返回 ``(since, until, 文案标签)``。

    默认取「本周一 ~ 现在」（周日晚上跑时就是本周）；传 ``week_start``
    可指定任意一天来补发历史周，此时 ``until`` 取该周周日 23:59:59。
    """
    ref = datetime.fromtimestamp(week_start if week_start is not None else time.time())
    start = (ref - timedelta(days=ref.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=7) - timedelta(seconds=1)
    until = end.timestamp() if week_start is not None else min(end.timestamp(), time.time())
    label = f"（{start:%m-%d} ~ {datetime.fromtimestamp(until):%m-%d}）"
    return start.timestamp(), until, label


def _lessons_by_student() -> dict[str, list[Any]]:
    """一次读完全部课次再按学生分组（班级群几十人时，避免反复全目录扫描）。"""
    from core.learning_stats import list_lessons  # noqa: PLC0415

    grouped: dict[str, list[Any]] = {}
    for metrics in list_lessons(limit=5000):
        grouped.setdefault(str(metrics.student_id), []).append(metrics)
    return grouped


async def push_weekly(
    student_id: str,
    *,
    since: float = 0.0,
    until: float = 0.0,
    label: str = "",
    dry_run: bool = False,
) -> int:
    """推送一名学生的周报（明细）：家长私聊 + 一对一家校群。

    班级群不在这里发 —— 每个学生发一条会把班级群刷屏，且天然发不出聚合口径；
    班级概览统一走 :func:`push_weekly_all`。
    """
    cfg = load_config()
    text = handlers.weekly_text(student_id, since=since, until=until, label=label)
    sent = await _send_to_parents(student_id, text, cfg, dry_run=dry_run)

    has_registry = groups_mod.GROUPS_PATH.is_file()
    for group in groups_mod.groups_for_student(student_id, for_weekly=True):
        if group.is_class:
            continue
        if await _send_group(group, text, dry_run=dry_run):
            sent += 1

    if not has_registry:
        if await _send_legacy_webhook(f"【周报】{student_id}\n{text}", cfg, dry_run=dry_run):
            sent += 1
    return sent


async def push_weekly_all(
    *,
    week_start: float | None = None,
    group_id: str = "",
    dry_run: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    """按群推送周报：班级群发聚合概览，一对一家校群发该生明细。

    返回每个群的执行结果（``scripts/push_weekly_reports.py`` 直接打印）。
    ``WECHAT_NOTIFY_WEEKLY`` 未开启时默认不发；``dry_run`` / ``force`` 可绕过。
    """
    cfg = load_config()
    if not cfg.notify_weekly and not (dry_run or force):
        log.info("WECHAT_NOTIFY_WEEKLY 未开启，跳过周报（可用 dry-run 预览或 --force 强发）")
        return []

    since, until, label = week_range(week_start)
    week_tag = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
    by_student = _lessons_by_student()
    results: list[dict[str, Any]] = []

    for group in groups_mod.load_groups():
        if group_id and group.id != group_id:
            continue
        if not group.weekly:
            results.append({"group": group.id, "name": group.name, "skipped": "该群未开启周报"})
            continue
        key = f"weekly:{group.id}:{week_tag}"
        if _already_pushed(key) and not dry_run:
            results.append({"group": group.id, "name": group.name, "skipped": "本周已推送"})
            continue

        if group.is_class:
            items = [
                metrics
                for sid in group.student_ids
                for metrics in by_student.get(sid, [])
                if since <= float(metrics.started_at) <= until
            ]
            if not items:
                results.append({
                    "group": group.id, "name": group.name, "kind": group.kind,
                    "skipped": "本周无上课记录",
                })
                continue
            text = handlers.class_weekly_text(
                group.name or group.id, label, items,
                min_students=cfg.class_weekly_min_students,
            )
            ok = await _send_group(group, text, dry_run=dry_run)
            results.append({
                "group": group.id, "name": group.name, "kind": group.kind,
                "sent": ok, "lessons": len(items), "text": text,
            })
            if ok and not dry_run:
                _mark_pushed(key)
            continue

        # 一对一家校群：每个学生一条明细
        sent_any = False
        parts: list[str] = []
        for sid in group.student_ids:
            items = [
                metrics for metrics in by_student.get(sid, [])
                if since <= float(metrics.started_at) <= until
            ]
            if not items:
                continue
            text = handlers.weekly_text(sid, since=since, until=until, label=label)
            parts.append(text)
            sent_any = await _send_group(group, text, dry_run=dry_run) or sent_any
        if not parts:
            results.append({
                "group": group.id, "name": group.name, "kind": group.kind,
                "skipped": "本周无上课记录",
            })
            continue
        results.append({
            "group": group.id, "name": group.name, "kind": group.kind,
            "sent": sent_any, "lessons": len(parts),
            "text": "\n\n".join(parts) if dry_run else "",
        })
        if sent_any and not dry_run:
            _mark_pushed(key)

    return results


# ---------------------------------------------------------------------- 预警
async def push_alert(student_id: str, message: str) -> int:
    """预警转人工：发给家长 + 配置的教师会话。"""
    cfg = load_config()
    text = f"【课堂提醒】{student_id}：{message}"
    sent = await _send_to_parents(student_id, text, cfg)
    if cfg.staff_chat_id:
        if await get_client().send_text(cfg.staff_chat_id, text):
            sent += 1
    elif cfg.webhook_enabled:
        if await get_client().send_group_text(text):
            sent += 1
    return sent


def pushed_before(key: str) -> bool:
    """公开版去重查询（供每日推送共用同一份 ``pushed.json``）。"""
    return _already_pushed(key)


def remember_pushed(key: str) -> None:
    """公开版去重记账。"""
    _mark_pushed(key)


def schedule_lesson_report(metrics: Any) -> None:
    """同步入口：把推送丢到后台，不阻塞下课流程。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(push_lesson_report(metrics))
        except Exception as err:  # noqa: BLE001
            log.warning("学情推送失败：{}", err)
        return
    try:
        loop.create_task(push_lesson_report(metrics))
    except Exception as err:  # noqa: BLE001
        log.warning("学情推送投递失败：{}", err)


__all__ = [
    "PUSH_STATE_KEEP",
    "push_alert",
    "push_lesson_report",
    "push_weekly",
    "push_weekly_all",
    "pushed_before",
    "remember_pushed",
    "schedule_lesson_report",
    "week_range",
]
