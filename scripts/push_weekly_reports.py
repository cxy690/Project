# -*- coding: utf-8 -*-
"""周学情推送（运维定时任务）。

按 ``data/wechat/groups.json`` 的群配置推送周报：

* 班级群（``kind=class``）→ **班级聚合概览**（不点名、不给单个学生分数）；
* 一对一家校群（``kind=parent``）→ 该生本周明细；
* 家长私聊（微信客服 / 公众号）→ 明细兜底。

用法：
    python scripts/push_weekly_reports.py --dry-run            # 只打印，不发
    python scripts/push_weekly_reports.py                      # 发本周（周一 ~ 现在）
    python scripts/push_weekly_reports.py --week 2026-10-14    # 补发该日期所在的那一周
    python scripts/push_weekly_reports.py --group g_class_3a   # 只发某个群
    python scripts/push_weekly_reports.py --force              # 忽略 WECHAT_NOTIFY_WEEKLY 开关

定时（云端 crontab，每周日 20:00）：
    0 20 * * 0 cd /root/autodl-tmp/education && \
      .venvs/app/bin/python scripts/push_weekly_reports.py >> logs/weekly_push.log 2>&1

退出码：0 = 全部成功或无事可做；1 = 有群发送失败；2 = 参数错误。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from wechat.notifier import push_weekly_all, week_range  # noqa: E402


def _parse_day(text: str) -> float | None:
    """把 ``YYYY-MM-DD`` / ``YYYYMMDD`` 解析成时间戳（本地时区）。"""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


async def _run(args: argparse.Namespace) -> int:
    week_start: float | None = None
    if args.week:
        week_start = _parse_day(args.week)
        if week_start is None:
            print(f"[错误] --week 需要 YYYY-MM-DD 格式：{args.week}")
            return 2

    since, until, label = week_range(week_start)
    mode = "预览（不发送）" if args.dry_run else "正式推送"
    print(f"[周区间] {label.strip('（）')}    模式：{mode}")
    if args.group:
        print(f"[限定群] {args.group}")

    results = await push_weekly_all(
        week_start=week_start,
        group_id=args.group,
        dry_run=args.dry_run,
        force=args.force,
    )
    if not results:
        print("[跳过] 没有可推送的群：检查 data/wechat/groups.json 是否存在、"
              "群是否开启 weekly，以及 WECHAT_NOTIFY_WEEKLY 是否为 true")
        return 0

    failed = 0
    for row in results:
        name = row.get("name") or row.get("group") or "?"
        gid = row.get("group") or "?"
        if row.get("skipped"):
            print(f"[SKIP] {gid} / {name}：{row['skipped']}")
            continue
        if row.get("sent"):
            print(f"[OK]   {gid} / {name}（{row.get('kind', '')}）课次={row.get('lessons', 0)}")
        else:
            failed += 1
            print(f"[FAIL] {gid} / {name}（{row.get('kind', '')}）发送失败，看 logs/ 里的 wechat 日志")
    print(f"[完成] 群 {len(results)} 个，失败 {failed} 个")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="按群推送周学情（班级群只发聚合概览）")
    parser.add_argument("--dry-run", action="store_true", help="只打印文案，不真的发")
    parser.add_argument("--week", default="", help="指定周内任意一天（YYYY-MM-DD），用于补发")
    parser.add_argument("--group", default="", help="只推某个群（groups.json 里的 id）")
    parser.add_argument("--force", action="store_true", help="忽略 WECHAT_NOTIFY_WEEKLY 开关")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
