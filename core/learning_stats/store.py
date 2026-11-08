"""学情数据落盘：``data/learning/`` 下的 jsonl + json。

目录约定::

    data/learning/
    ├── lessons/
    │   ├── <lesson_id>.jsonl        # 原始事件流（只追加，审计用）
    │   ├── <lesson_id>.meta.json    # 课程元信息（学生/学科/计划时长）
    │   └── <lesson_id>.metrics.json # 指标快照（可随时由 jsonl 重算）
    └── students/
        └── <student_id>.json        # 学生最近若干节摘要（家长/老师列表页用）

为什么用 jsonl 而不是直接进 MySQL：课堂事件是**高频小写入**，走主库会拖慢答疑
主链路；而学情数据的查询是"按学生看最近几节"，量级很小，文件足够。
（``db/`` 里的 users / sessions 表仍由既有链路维护，两者互不依赖。）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from config.logging import get_logger
from core.learning_stats.models import LessonEvent, LessonMetrics

log = get_logger("core.learning_stats.store")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data" / "learning"
LESSON_DIR = DATA_ROOT / "lessons"
STUDENT_DIR = DATA_ROOT / "students"

#: 学生索引里保留的最近课次数
STUDENT_KEEP = 30

_SAFE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")


def safe_name(value: str) -> str:
    """把 session_id / student_id 变成安全文件名（保留中文与常见符号）。"""
    text = _SAFE.sub("_", str(value or "").strip())
    return text[:120] or "unknown"


def new_lesson_id(session_id: str, ts: float | None = None) -> str:
    """生成课次 id：``20261019-165530_<session>``（按时间可排序）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts or time.time()))
    return f"{stamp}_{safe_name(session_id)}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 单个坏文件不影响整体
        return {}


# ---------------------------------------------------------------- 事件流
def append_event(lesson_id: str, event: LessonEvent) -> None:
    """追加一条事件（jsonl 单行，崩溃最多丢最后一行）。"""
    LESSON_DIR.mkdir(parents=True, exist_ok=True)
    path = LESSON_DIR / f"{safe_name(lesson_id)}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")


def read_events(lesson_id: str) -> list[LessonEvent]:
    path = LESSON_DIR / f"{safe_name(lesson_id)}.jsonl"
    if not path.is_file():
        return []
    events: list[LessonEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(LessonEvent.from_dict(json.loads(line)))
        except Exception:  # noqa: BLE001 - 坏行跳过
            continue
    return events


def write_meta(lesson_id: str, meta: dict[str, Any]) -> None:
    _write_json(LESSON_DIR / f"{safe_name(lesson_id)}.meta.json", meta)


def read_meta(lesson_id: str) -> dict[str, Any]:
    return _read_json(LESSON_DIR / f"{safe_name(lesson_id)}.meta.json")


# ---------------------------------------------------------------- 指标
def write_metrics(metrics: LessonMetrics) -> None:
    _write_json(
        LESSON_DIR / f"{safe_name(metrics.lesson_id)}.metrics.json",
        metrics.to_dict(),
    )
    _upsert_student_index(metrics)


def read_metrics(lesson_id: str) -> LessonMetrics | None:
    raw = _read_json(LESSON_DIR / f"{safe_name(lesson_id)}.metrics.json")
    if not raw:
        return None
    try:
        return LessonMetrics.from_dict(raw)
    except Exception as err:  # noqa: BLE001
        log.warning("学情指标解析失败：{}（{}）", lesson_id, err)
        return None


def list_lessons(student_id: str = "", limit: int = 50) -> list[LessonMetrics]:
    """按开始时间倒序列出课次（可按学生过滤）。"""
    if not LESSON_DIR.is_dir():
        return []
    items: list[LessonMetrics] = []
    for path in LESSON_DIR.glob("*.metrics.json"):
        metrics = read_metrics(path.name.removesuffix(".metrics.json"))
        if metrics is None:
            continue
        if student_id and metrics.student_id != student_id:
            continue
        items.append(metrics)
    items.sort(key=lambda m: (m.started_at, m.lesson_id), reverse=True)
    return items[: max(1, int(limit))]


def _upsert_student_index(metrics: LessonMetrics) -> None:
    """更新学生索引：最近 N 节摘要 + 汇总分。"""
    student_id = metrics.student_id
    if not student_id:
        return
    path = STUDENT_DIR / f"{safe_name(student_id)}.json"
    data = _read_json(path) or {}
    lessons: list[dict[str, Any]] = list(data.get("lessons") or [])
    row = {
        "lesson_id": metrics.lesson_id,
        "subject": metrics.subject,
        "stage": metrics.stage,
        "started_at": metrics.started_at,
        "completion_score": metrics.completion_score,
        "mastery_score": metrics.mastery_score,
        "interaction_rate": metrics.interaction_rate,
        "authenticity_score": metrics.authenticity_score,
        "overall_score": metrics.overall_score,
        "flags": metrics.flags,
    }
    lessons = [item for item in lessons if item.get("lesson_id") != metrics.lesson_id]
    lessons.insert(0, row)
    lessons = lessons[:STUDENT_KEEP]
    totals = {
        "lessons": len(lessons),
        "avg_overall": round(sum(float(i.get("overall_score") or 0) for i in lessons) / len(lessons), 1)
        if lessons else 0.0,
        "avg_mastery": round(sum(float(i.get("mastery_score") or 0) for i in lessons) / len(lessons), 1)
        if lessons else 0.0,
    }
    _write_json(path, {
        "student_id": student_id,
        "updated_at": time.time(),
        "totals": totals,
        "lessons": lessons,
    })


def read_student_index(student_id: str) -> dict[str, Any]:
    return _read_json(STUDENT_DIR / f"{safe_name(student_id)}.json")


def list_students(limit: int = 200) -> list[dict[str, Any]]:
    """所有有学情记录的学生（按最近更新时间倒序）。"""
    if not STUDENT_DIR.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in STUDENT_DIR.glob("*.json"):
        data = _read_json(path)
        if data.get("student_id"):
            rows.append(data)
    rows.sort(key=lambda d: float(d.get("updated_at") or 0), reverse=True)
    return rows[: max(1, int(limit))]


def iter_lesson_ids(student_id: str = "") -> Iterable[str]:
    """遍历课次 id（重算/迁移用）。"""
    for metrics in list_lessons(student_id, limit=10_000):
        yield metrics.lesson_id
