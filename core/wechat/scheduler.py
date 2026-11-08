"""家长日报定时器：每天到点后批量发送，发送记录负责幂等。"""
from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from config.logging import get_logger
from config.settings import settings
from core.wechat.service import send_all_today

log = get_logger("core.wechat.scheduler")


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(settings.wechat_report_timezone or "Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return datetime.now()


def _report_time() -> dtime:
    raw = str(settings.wechat_report_time or "20:30").strip()
    try:
        hour, minute = raw.split(":", 1)
        return dtime(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError):
        return dtime(20, 30)


def run_once(now: datetime | None = None, *, force: bool = False) -> list[dict[str, Any]]:
    """到点后发送当天日报；未到点返回空列表。"""
    if not settings.wechat_assistant_enable or not settings.wechat_scheduler_enable:
        return []
    current = now or _now()
    if current.timetz().replace(tzinfo=None) < _report_time() and not force:
        return []
    day = current.date().isoformat()
    results = send_all_today(day, force=force)
    sent = sum(1 for item in results if item.get("status") == "sent")
    failed = sum(1 for item in results if item.get("status") == "failed")
    if results:
        log.info("微信家长日报：{}，发送 {} 条，失败 {} 条", day, sent, failed)
    return results


async def _poll(action, label: str, stop: asyncio.Event | None) -> None:
    interval = max(10, int(settings.wechat_scheduler_interval_seconds or 60))
    while stop is None or not stop.is_set():
        try:
            await asyncio.to_thread(action)
        except Exception as err:  # noqa: BLE001
            log.warning("{}定时任务异常：{}", label, type(err).__name__)
        try:
            if stop is None:
                await asyncio.sleep(interval)
            else:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
        except asyncio.TimeoutError:
            continue


async def run_forever(stop: asyncio.Event | None = None) -> None:
    """日报与课程独立轮询，避免长日报批次阻塞课程通知时段。"""
    from core.wechat.course_robot import run_due
    from core.wechat.user_notices import run_due as run_user_notices

    tasks = [asyncio.create_task(_poll(run_once, "微信日报", stop)),
             asyncio.create_task(_poll(run_due, "微信课程通知", stop)),
             asyncio.create_task(_poll(run_user_notices, "用户课程更新", stop))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["run_forever", "run_once"]
