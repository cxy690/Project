"""学情事件与报告的落盘层。

为什么要单独一层：
* 课堂里已经有 ``ai_quiz`` / ``ai_check`` / ``course_state`` 等事件，但它们只
  ``push_event`` 给前端，连接一断就消失；
* 学情分析必须能事后回答「这节课学生做了什么、有没有真的学会」，因此需要
  append-only 的事件流；
* 事件按天滚动，报告按会话缓存，沿用项目里 ``memory`` / ``verdict`` 的轻量
  文件存储方式，避免在 MySQL 不可用时拖垮课堂主链路。

目录约定::

    <LEARNING_DIR>/
      events/learning-YYYY-MM-DD.jsonl    # 一行一个事件（append-only）
      reports/<session_id>.json          # 会话结束后聚合的报告
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import settings

log = get_logger("core.learning.store")

_EVENTS_DIR = "events"
_REPORTS_DIR = "reports"
_EVENT_PREFIX = "learning-"
_SAFE_ID = re.compile(r"[^0-9A-Za-z_.-]+")
_LOCK = threading.Lock()


def _root() -> Path:
    return Path(settings.learning_dir).expanduser()


def _events_dir() -> Path:
    return _root() / _EVENTS_DIR


def _reports_dir() -> Path:
    return _root() / _REPORTS_DIR


def _day_path(ts: float) -> Path:
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return _events_dir() / f"{_EVENT_PREFIX}{day}.jsonl"


def _safe_session_id(session_id: str) -> str:
    safe = _SAFE_ID.sub("_", str(session_id or "").strip())
    return safe[:120] or "unknown"


def append_event(event: dict[str, Any]) -> bool:
    """追加一条事件；写盘失败只记日志，绝不影响课堂。"""
    if not settings.learning_analytics_enable:
        return False
    try:
        ts = float(event.get("ts") or time.time())
        path = _day_path(ts)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception as err:  # noqa: BLE001 - 观测层不能拖垮教学
        log.warning("学情事件写入失败：{}", err)
        return False


def _event_files(days: int | None = None) -> list[Path]:
    root = _events_dir()
    if not root.is_dir():
        return []
    files = sorted(root.glob(f"{_EVENT_PREFIX}*.jsonl"))
    if days is None or days <= 0:
        return files
    cutoff = date.today() - timedelta(days=max(1, int(days)))
    picked: list[Path] = []
    for path in files:
        raw = path.stem.replace(_EVENT_PREFIX, "")
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            picked.append(path)
    return picked


def load_events(
    *,
    days: int | None = None,
    student_id: str = "",
    session_id: str = "",
    limit: int = 0,
) -> list[dict[str, Any]]:
    """读取事件（按时间升序）；坏行跳过。"""
    if days is None:
        days = int(settings.learning_retention_days)
    events: list[dict[str, Any]] = []
    for path in _event_files(days):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict) or not item.get("event_type"):
                        continue
                    if student_id and str(item.get("student_id") or "") != student_id:
                        continue
                    if session_id and str(item.get("session_id") or "") != session_id:
                        continue
                    events.append(item)
        except OSError as err:
            log.warning("学情事件读取失败 {}：{}", path, err)
    events.sort(key=lambda item: float(item.get("ts") or 0.0))
    if limit > 0:
        events = events[-limit:]
    return events


def list_session_ids(
    *,
    days: int | None = None,
    student_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """列出最近出现过的会话（session_id / student_id / 首末时间）。"""
    seen: dict[str, dict[str, Any]] = {}
    for item in load_events(days=days, student_id=student_id):
        sid = str(item.get("session_id") or "").strip()
        if not sid:
            continue
        ts = float(item.get("ts") or 0.0)
        row = seen.get(sid)
        if row is None:
            seen[sid] = {
                "session_id": sid,
                "student_id": str(item.get("student_id") or ""),
                "subject": str(item.get("subject") or ""),
                "stage": str(item.get("stage") or ""),
                "started_at": ts,
                "last_at": ts,
            }
        else:
            row["last_at"] = max(float(row.get("last_at") or 0.0), ts)
            if not row.get("subject") and item.get("subject"):
                row["subject"] = str(item.get("subject") or "")
            if not row.get("stage") and item.get("stage"):
                row["stage"] = str(item.get("stage") or "")
    rows = sorted(seen.values(), key=lambda row: float(row.get("last_at") or 0.0), reverse=True)
    return rows[: max(1, int(limit))]


def save_report(report: dict[str, Any]) -> Path | None:
    """保存会话报告（覆盖写；失败只记日志）。"""
    if not settings.learning_analytics_enable:
        return None
    try:
        sid = _safe_session_id(str(report.get("session_id") or ""))
        path = _reports_dir() / f"{sid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception as err:  # noqa: BLE001
        log.warning("学情报告写入失败：{}", err)
        return None


def load_report(session_id: str) -> dict[str, Any] | None:
    """读取已缓存的会话报告；不存在/损坏返回 None。"""
    path = _reports_dir() / f"{_safe_session_id(session_id)}.json"
    if not path.is_file():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return item if isinstance(item, dict) else None
