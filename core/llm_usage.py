"""LLM token 用量计量（L0 数据观测层）：把每次模型调用的 token 消耗落盘。

为什么需要它
------------
厂商的响应里**本来就返回了 usage**（prompt / completion / total tokens），
但它随响应发完就丢 —— 于是「这个月烧了多少 token」「哪天用量突然翻倍」
「意图识别小模型值不值」这些问题全都没有数据可查。

本模块只做一件事：把响应里现成的 usage 抄一份到 ``data/raw/llm_usage/``
（按天一个 append-only JSONL）。**不新增模型调用、不改提示词、不改任何行为**。

设计取舍（与 :mod:`core.verdict` / :mod:`core.safety_audit` 同构）
------------------------------------------------------------------
1. **按天一个文件**（``usage-YYYY-MM-DD.jsonl``）：日期范围聚合 = 读几天的文件，
   不需要索引；单文件损坏也只损失一天。
2. **一行一条 JSONL**：只追加、永不整体重写；人可以直接 ``grep`` / ``tail`` 看。
3. **写失败必须静默**：观测层绝不能因为磁盘问题把答疑搞挂。
4. **拿不到 usage 就不记**（部分厂商不返回）：宁缺毋滥，避免造出全零的脏数据。
5. **场景靠调用链推断**（``core.trace`` 现成可用）：不为记账去改一圈调用点；
   推断不出的记 ``other``，不影响金额口径。

「来源」是怎么推断的
--------------------
LLM 调用发生在答疑 / 课件 / 意图分类等不同链路里，本模块读 ``core.trace``
当前回合的步骤栈，用「最内层步骤所在文件 + 函数名」匹配 :data:`_SOURCE_RULES`。
trace 关闭（``TRACE_ENABLE=false``）时推断不出，全部记 ``other`` —— 只影响
「哪个场景花的」，总量不受影响。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import settings

log = get_logger("core.llm_usage")

#: 单次查询允许的最大日期跨度（防呆：公开接口不能让一次请求去扫几年的数据）。
MAX_RANGE_DAYS = 90

#: 场景映射规则：``(文件前缀, 函数名关键词, 场景 key)``，**顺序即优先级**。
#: 函数名关键词为空 = 该文件的兜底规则；关键词优先于同文件的兜底规则
#: （如 agents/teacher 里的出题 / 判题要先于「答疑」兜底被匹配到）。
_SOURCE_RULES: tuple[tuple[str, str, str], ...] = (
    ("core/intent/", "", "intent"),                    # 意图识别小模型
    ("agents/lecturer/", "", "courseware"),            # 课件讲解
    ("agents/teacher/agent.py", "generate_quiz", "quiz"),
    ("agents/teacher/agent.py", "evaluate", "judge"),
    ("agents/teacher/", "", "teach"),                  # 答疑主链路（含 ReAct 闭环）
    ("agents/supervisor/", "", "supervisor"),          # 空闲问候等课堂调度
    ("core/skills/experience/", "", "reflect"),        # 课后复盘 / 经验去重
    ("core/skills/expression_skill.py", "", "expression"),  # 表情判定
    ("core/planning/", "", "planner"),                 # 回合执行规划
    ("core/memory/", "", "memory"),                    # 学情记忆蒸馏
    ("core/failure_cluster.py", "", "diagnosis"),      # 失败根因分析
    ("core/health.py", "", "diagnosis"),
    ("document_parsing/", "", "parsing"),              # 题目图片解析
)

#: 场景 key -> 中文标签（接口直接返回，前端只负责画）。
SOURCE_LABELS: dict[str, str] = {
    "teach": "答疑讲解",
    "courseware": "课件讲解",
    "intent": "意图识别",
    "quiz": "出题",
    "judge": "判题",
    "reflect": "课后复盘",
    "planner": "执行规划",
    "supervisor": "课堂调度",
    "expression": "表情判定",
    "memory": "学情记忆",
    "diagnosis": "健康诊断",
    "parsing": "题目解析",
    "other": "其他",
}


# ---------------------------------------------------------------------- 采集
def infer_source(steps: list[Any] | None = None) -> str:
    """从调用链推断本次模型调用属于哪个场景（best-effort，推不出返回 ``other``）。

    ``steps`` 传空/None 时现取 ``core.trace`` 当前回合的步骤栈；取不到（trace 关闭、
    不在回合内）就返回 ``other`` —— 少了维度标签，但 token 总量照样准确。
    """
    if steps is None:
        try:
            from core.trace import current_steps  # noqa: PLC0415 - 观测层可选，避免导入期耦合

            steps = current_steps()
        except Exception:  # noqa: BLE001 - 拿不到调用链不该影响记账
            steps = []
    # 从最内层往外看：越靠近模型调用的那一层越能说明「谁在调」
    for step in reversed(list(steps or [])):
        file = str(getattr(step, "file", "") or "")
        func = str(getattr(step, "func", "") or "")
        for prefix, keyword, source in _SOURCE_RULES:
            if file.startswith(prefix) and (not keyword or keyword in func):
                return source
    return "other"


def _as_tokens(value: Any) -> int:
    """把厂商返回的 token 计数归一化成非负整数（缺字段 / 脏值一律 0）。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cached_tokens(usage: dict[str, Any]) -> int:
    """兼容各家「缓存命中 token」的字段名（没有就 0）。

    * OpenAI：``prompt_tokens_details.cached_tokens``；
    * DeepSeek：``prompt_cache_hit_tokens``。
    缓存命中的部分通常按更低价格计费，单独留一列，别把它混进 prompt 里看不出门道。
    """
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _as_tokens(details.get("cached_tokens"))
        if cached:
            return cached
    return _as_tokens(usage.get("prompt_cache_hit_tokens"))


@dataclass
class _Agg:
    """一个分组（模型 / 场景 / 天）上的累计值。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def add(self, rec: dict[str, Any]) -> None:
        self.calls += 1
        self.prompt_tokens += _as_tokens(rec.get("prompt_tokens"))
        self.completion_tokens += _as_tokens(rec.get("completion_tokens"))
        self.total_tokens += _as_tokens(rec.get("total_tokens"))
        self.cached_tokens += _as_tokens(rec.get("cached_tokens"))

    def as_dict(self, *, key: str = "", label: str = "", share: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }
        if key:
            out["key"] = key
        if label:
            out["label"] = label
        if share is not None:
            # 占比恒给数值（0.0 也要给）：前端直接画，不用处理「字段有时没有」的分支
            out["share_pct"] = round(100.0 * share, 1)
        return out


def build_report(records: list[dict[str, Any]], *, since: str, until: str) -> dict[str, Any]:
    """把日期范围内的记录聚合成报告（**纯计算**，方便单测直接喂数据）。

    结构（前端只负责画）：

    * ``totals``    —— 总量：调用次数 + 三类 token + 缓存命中；
    * ``daily``     —— 逐日序列（**缺失日期补零**，保证图中 X 轴连续）；
    * ``by_model``  —— 按模型分组（降序）；
    * ``by_source`` —— 按场景分组（降序，带中文标签）。
    """
    totals = _Agg()
    by_model: dict[str, _Agg] = {}
    by_source: dict[str, _Agg] = {}
    by_day: dict[str, _Agg] = {}

    for rec in records:
        if not isinstance(rec, dict):
            continue
        totals.add(rec)
        model = str(rec.get("model") or "未知模型")
        source = str(rec.get("source") or "other")
        day = str(rec.get("day") or "")
        by_model.setdefault(model, _Agg()).add(rec)
        by_source.setdefault(source, _Agg()).add(rec)
        if day:
            by_day.setdefault(day, _Agg()).add(rec)

    def _rows(groups: dict[str, _Agg], *, label_of=lambda k: k) -> list[dict[str, Any]]:
        rows = [
            agg.as_dict(key=key, label=label_of(key),
                        share=(agg.total_tokens / totals.total_tokens) if totals.total_tokens else 0.0)
            for key, agg in groups.items()
        ]
        rows.sort(key=lambda r: (-r["total_tokens"], -r["calls"], r["key"]))
        return rows

    daily: list[dict[str, Any]] = []
    for day in iter_days(since, until):
        agg = by_day.get(day)
        row = (agg or _Agg()).as_dict()
        row["day"] = day
        daily.append(row)

    return {
        "range": {"since": since, "until": until, "days": len(daily)},
        "totals": totals.as_dict(),
        "daily": daily,
        "by_model": _rows(by_model),
        "by_source": _rows(by_source, label_of=lambda k: SOURCE_LABELS.get(k, k)),
    }


# ---------------------------------------------------------------------- 日期
def parse_day(text: str | None) -> date | None:
    """把 ``YYYY-MM-DD`` 解析成 date；不合法返回 ``None``（调用方自行兜底）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def iter_days(since: str, until: str) -> list[str]:
    """``[since, until]`` 内的日期串（升序）；非法输入返回空列表。"""
    start, end = parse_day(since), parse_day(until)
    if start is None or end is None or start > end:
        return []
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def resolve_range(since: str = "", until: str = "", days: int = 7) -> tuple[str, str]:
    """把接口参数归一化成 ``(since, until)``：显式日期优先，否则按最近 N 天。

    防呆（接口是公开的，不能让一次请求扫几年的数据）：跨度上限
    :data:`MAX_RANGE_DAYS` 天，超出自动收窄到「以 until 结尾的最近 90 天」。
    """
    today = datetime.now().date()
    span = max(1, min(MAX_RANGE_DAYS, int(days or 7)))
    end = parse_day(until) or today
    start = parse_day(since) or (end - timedelta(days=span - 1))
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS - 1)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------- 存储
class UsageStore:
    """token 用量的 JSONL 存储（按天分文件，只追加）。

    ``directory`` 留空时**每次读写都现取** ``settings.llm_usage_dir``（而不是在构造时
    固定）：测试可 monkeypatch 换目录，生产可改 ``.env`` —— 模块级单例是在 import 期
    构造的，若在构造时固定目录，之后任何配置变更都会「看起来改了其实没生效」。
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self._fixed_dir = Path(directory) if directory is not None else None
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    @property
    def dir(self) -> Path:
        """落盘目录（现取现建，保证首次写入前目录一定存在）。"""
        base = self._fixed_dir or Path(settings.llm_usage_dir)
        base.mkdir(parents=True, exist_ok=True)
        return base

    def path_for(self, day: str) -> Path:
        """某一天的落盘文件（``usage-YYYY-MM-DD.jsonl``）。"""
        return self.dir / f"usage-{day}.jsonl"

    # ---------------------------------------------------------------- 写
    def record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """追加一条用量记录；关闭或写失败时返回 ``None``（**绝不抛**）。"""
        if not settings.llm_usage_enable:
            return None
        now = time.time()
        entry = {
            "ts": now,
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            **payload,
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock, self.path_for(entry["day"]).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except (OSError, TypeError, ValueError) as err:
            log.warning("token 用量落盘失败（不影响主链路）：{}", err)
            return None
        self._cleanup_if_due(now)
        return entry

    # ---------------------------------------------------------------- 读
    def read_range(self, since: str, until: str) -> list[dict[str, Any]]:
        """读 ``[since, until]`` 范围内的记录。单行损坏只跳过该行，不放弃整个文件。"""
        out: list[dict[str, Any]] = []
        for day in iter_days(since, until):
            path = self.path_for(day)
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as err:
                log.warning("token 用量读取失败（{}）：{}", day, err)
                continue
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    # 写了一半被 kill 的行不应让整天的数据不可读
                    log.debug("跳过损坏的用量行：{}", raw[:60])
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    def report(self, since: str, until: str) -> dict[str, Any]:
        """读 + 聚合一步到位（接口直接返回它的结果）。"""
        return build_report(self.read_range(since, until), since=since, until=until)

    def _cleanup_if_due(self, now: float) -> None:
        """按 ``llm_usage_keep_days`` 清理过期文件（每小时最多做一次）。"""
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        keep = max(int(settings.llm_usage_keep_days or 0), 1)
        cutoff = (datetime.now() - timedelta(days=keep)).strftime("%Y-%m-%d")
        try:
            for path in self.dir.glob("usage-*.jsonl"):
                day = path.stem.removeprefix("usage-")
                if day < cutoff:
                    path.unlink(missing_ok=True)
                    log.info("清理过期用量文件：{}", path.name)
        except OSError as err:  # noqa: BLE001 - 清理失败无所谓
            log.debug("用量清理失败：{}", err)


#: 进程内共享单例（与 ``core.verdict.verdict_store`` 同一用法）：
#: 共用一份目录与同一把锁，避免各会话各写一份导致交错。
usage_store = UsageStore()


def get_usage_store() -> UsageStore:
    """返回进程级用量存储（测试可换目录后直接复用）。"""
    return usage_store


def record_llm_usage(
    *,
    model: str,
    usage: Any,
    endpoint: str = "chat",
) -> dict[str, Any] | None:
    """采集入口：把一次模型响应的 usage 落盘（**一次调用最多记一行，绝不抛异常**）。

    由 :mod:`models.llm` 在各条完成路径上调用。``usage`` 不是字典 /
    token 全为零（厂商没返回）时直接跳过 —— 不记空账。
    """
    if not settings.llm_usage_enable or not isinstance(usage, dict):
        return None
    try:
        prompt = _as_tokens(usage.get("prompt_tokens"))
        completion = _as_tokens(usage.get("completion_tokens"))
        total = _as_tokens(usage.get("total_tokens")) or (prompt + completion)
        if prompt <= 0 and completion <= 0 and total <= 0:
            return None
        return usage_store.record({
            "day": time.strftime("%Y-%m-%d"),
            "model": str(model or ""),
            "endpoint": str(endpoint or "chat"),
            "source": infer_source(),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cached_tokens": _cached_tokens(usage),
        })
    except Exception as err:  # noqa: BLE001 - 观测层任何异常都必须被吞掉
        log.warning("token 用量采集失败（不影响主链路）：{}", err)
        return None


__all__ = [
    "MAX_RANGE_DAYS",
    "SOURCE_LABELS",
    "UsageStore",
    "build_report",
    "get_usage_store",
    "infer_source",
    "iter_days",
    "parse_day",
    "record_llm_usage",
    "resolve_range",
    "usage_store",
]
