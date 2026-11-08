# -*- coding: utf-8 -*-
"""每日学情推送（手动 / 补发 / 预览）。

正式运行由应用内的后台任务在 ``WECHAT_PUSH_HOUR``（默认 22:00）自动触发，
本脚本用于：上线前预览文案、补发某一天、只推某个群。

用法：
    python scripts/push_daily_reports.py --dry-run                 # 只打印文案
    python scripts/push_daily_reports.py --dry-run --date 2026-10-21  # 预览指定日期
    python scripts/push_daily_reports.py                           # 补发今天（受去重保护）
    python scripts/push_daily_reports.py --force --date 2026-10-21 # 忽略去重强发
    python scripts/push_daily_reports.py --group g_test            # 只推某个群
    python scripts/push_daily_reports.py --no-weekly               # 周日也不合并周报

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

from wechat.daily import push_daily_all  # noqa: E402


def _parse_day(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


async def _run(args: argparse.Namespace) -> int:
    now: datetime | None = None
    if args.date:
        parsed = _parse_day(args.date)
        if parsed is None:
            print(f"[错误] --date 需要 YYYY-MM-DD 格式：{args.date}")
            return 2
        # 补发某天时按当天 23:59 结算，保证当天的课都算进去
        now = parsed.replace(hour=23, minute=59, second=0, microsecond=0)

    mode = "预览（不发送）" if args.dry_run else "正式推送"
    print(f"[推送时刻] {(now or datetime.now()):%Y-%m-%d %H:%M}    模式：{mode}")
    if args.group:
        print(f"[限定群] {args.group}")

    results = await push_daily_all(
        now=now,
        dry_run=args.dry_run,
        force=args.force,
        include_weekly=False if args.no_weekly else None,
        group_id=args.group,
    )
    if not results:
        print("[跳过] 没有可推送的群：检查 data/wechat/groups.json 是否存在、"
              "群是否开启 daily，以及 WECHAT_NOTIFY_DAILY 是否为 true")
        return 0

    failed = 0
    for row in results:
        name = row.get("name") or row.get("group") or "?"
        gid = row.get("group") or "?"
        if row.get("skipped"):
            print(f"[SKIP] {gid} / {name}：{row['skipped']}")
            continue
        if row.get("text"):
            print("\n" + "=" * 60)
            print(f"[{gid} / {name}]（{row.get('kind', '')}，{row.get('lessons', 0)} 节）")
            print("-" * 60)
            print(row["text"])
            print("=" * 60 + "\n")
        if row.get("sent"):
            flag = "含周报" if row.get("weekly") else "日报"
            print(f"[OK]   {gid} / {name}（{row.get('kind', '')}）{flag}，课次={row.get('lessons', 0)}")
        else:
            failed += 1
            print(f"[FAIL] {gid} / {name}：{row.get('error') or '发送失败'}（看 logs/ 的 wechat 日志）")
    print(f"[完成] 群 {len(results)} 个，失败 {failed} 个")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="推送当日学情（周日自动合并本周周报）")
    parser.add_argument("--dry-run", action="store_true", help="只打印文案，不真的发")
    parser.add_argument("--date", default="", help="补发指定日期（YYYY-MM-DD）")
    parser.add_argument("--group", default="", help="只推某个群（groups.json 里的 id）")
    parser.add_argument("--force", action="store_true", help="忽略开关与去重，强制发送")
    parser.add_argument("--no-weekly", action="store_true", help="周日也不合并本周周报")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
