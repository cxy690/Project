"""token 用量计量（``core/llm_usage``）专项测试。

钉住五件事：

1. **采集**：厂商 usage 归一化（缺字段 / 脏值 / 缓存命中兼容 / 全零不记 /
   开关关闭不记），任何异常都不上抛；
2. **落盘**：按天分文件、一行一条 JSONL、坏行只跳过不放弃整天；
3. **聚合**：日期范围过滤、缺失日期补零、按模型 / 按场景分组与占比；
4. **场景推断**：从调用链文件 / 函数名推断来源，推不出记 ``other``；
5. **接口**：``/api/admin/llm/usage`` 的门禁（未登录 401 / 非 admin 403 / admin 放行）
   与日期筛选、跨度防呆（上限 90 天）。

隔离：``LLM_USAGE_DIR`` 切到 tmp_path（绝不写真实 ``data/raw/llm_usage``）；
数据库用临时 SQLite（同 test_skill_admin 的约定）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from config.settings import settings
from core.llm_usage import (
    MAX_RANGE_DAYS,
    UsageStore,
    build_report,
    infer_source,
    iter_days,
    parse_day,
    record_llm_usage,
    resolve_range,
)


class _Step:
    """假调用链步骤（只带推断需要的两个字段）。"""

    def __init__(self, file: str = "", func: str = "") -> None:
        self.file = file
        self.func = func


@pytest.fixture()
def usage_dir(tmp_path, monkeypatch):
    """把用量目录切到 tmp_path 并确保开关打开（绝不触碰真实数据目录）。"""
    target = tmp_path / "llm_usage"
    monkeypatch.setattr(settings, "llm_usage_enable", True)
    monkeypatch.setattr(settings, "llm_usage_dir", str(target))
    return target


def _day(offset: int = 0) -> str:
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 采集
def test_record_normalizes_usage(usage_dir) -> None:
    """正常 usage：落盘一行，字段齐全（含场景推断，trace 不在回合内 → other）。"""
    entry = record_llm_usage(
        model="deepseek-chat",
        usage={"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540},
    )
    assert entry is not None
    assert entry["prompt_tokens"] == 1200 and entry["completion_tokens"] == 340
    assert entry["total_tokens"] == 1540
    assert entry["model"] == "deepseek-chat" and entry["endpoint"] == "chat"
    assert entry["source"] == "other" and entry["day"] == _day()

    path = usage_dir / f"usage-{_day()}.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["total_tokens"] == 1540


def test_record_skips_bad_or_empty_usage(usage_dir) -> None:
    """拿不到 usage / 全零 / 脏值不应造出脏账：能修则修，修不了就跳过。"""
    assert record_llm_usage(model="m", usage=None) is None
    assert record_llm_usage(model="m", usage="oops") is None
    assert record_llm_usage(model="m", usage={"prompt_tokens": 0, "completion_tokens": 0}) is None

    # total 缺失 → 用 prompt + completion 补；脏值（负数 / 非数字）归零
    entry = record_llm_usage(
        model="m",
        usage={"prompt_tokens": "100", "completion_tokens": -5, "total_tokens": "abc"},
    )
    assert entry is not None and entry["prompt_tokens"] == 100
    assert entry["completion_tokens"] == 0 and entry["total_tokens"] == 100


def test_record_reads_vendor_cached_tokens(usage_dir) -> None:
    """缓存命中兼容 OpenAI 与 DeepSeek 两种字段名。"""
    a = record_llm_usage(model="m", usage={
        "prompt_tokens": 100, "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 64},
    })
    b = record_llm_usage(model="m", usage={
        "prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 32,
    })
    assert a and a["cached_tokens"] == 64
    assert b and b["cached_tokens"] == 32


def test_record_disabled_by_switch(usage_dir, monkeypatch) -> None:
    """总开关关掉 = 完全不落盘（连目录也不用建）。"""
    monkeypatch.setattr(settings, "llm_usage_enable", False)
    assert record_llm_usage(model="m", usage={"total_tokens": 5}) is None
    assert not (usage_dir / f"usage-{_day()}.jsonl").exists()


# ---------------------------------------------------------------- 聚合
def test_report_aggregates_by_day_model_source() -> None:
    """纯聚合：逐日补零、按模型 / 场景分组、占比与排序。"""
    d0, d1 = _day(-1), _day()
    records = [
        {"day": d0, "model": "a", "source": "teach", "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        {"day": d1, "model": "a", "source": "teach", "prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        {"day": d1, "model": "b", "source": "intent", "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    ]
    report = build_report(records, since=d0, until=d1)

    assert report["totals"]["calls"] == 3
    assert report["totals"]["total_tokens"] == 195
    assert [row["total_tokens"] for row in report["daily"]] == [120, 75]
    assert report["daily"][0]["day"] == d0

    assert [r["key"] for r in report["by_model"]] == ["a", "b"]           # 用量降序
    assert report["by_model"][0]["share_pct"] == round(180 / 195 * 100, 1)
    sources = {r["key"]: r for r in report["by_source"]}
    assert sources["intent"]["label"] == "意图识别"                        # 中文标签在服务端给
    assert sources["teach"]["calls"] == 2


def test_report_pads_missing_days() -> None:
    """范围内没有记录的那天也要出现在序列里（补零），前端画图 X 轴才连续。"""
    report = build_report([], since=_day(-3), until=_day())
    assert [row["day"] for row in report["daily"]] == [_day(-3), _day(-2), _day(-1), _day()]
    assert all(row["total_tokens"] == 0 and row["calls"] == 0 for row in report["daily"])
    assert report["totals"]["total_tokens"] == 0 and report["by_model"] == []


def test_store_skips_corrupt_lines(tmp_path) -> None:
    """坏行（写一半被 kill）只跳过该行，不放弃整天。"""
    store = UsageStore(tmp_path)
    day = _day()
    path = store.path_for(day)
    path.write_text(
        json.dumps({"day": day, "model": "m", "total_tokens": 7}, ensure_ascii=False) + "\n"
        + "{坏行\n"
        + json.dumps({"day": day, "model": "m", "total_tokens": 3}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    records = store.read_range(day, day)
    assert [r["total_tokens"] for r in records] == [7, 3]


# ---------------------------------------------------------------- 场景推断
def test_infer_source_from_trace_steps() -> None:
    """按「最内层步骤的文件 + 函数名」推断场景；推不出记 other。"""
    assert infer_source([_Step("core/intent/classifier.py", "IntentLlmClassifier.classify")]) == "intent"
    assert infer_source([_Step("agents/lecturer/agent.py", "LecturerAgent.explain_slide")]) == "courseware"
    assert infer_source([_Step("agents/teacher/agent.py", "TeacherAgent.answer")]) == "teach"
    # 同文件里的出题 / 判题优先于「答疑」兜底规则
    assert infer_source([_Step("agents/teacher/agent.py", "TeacherAgent.generate_quiz")]) == "quiz"
    assert infer_source([_Step("agents/teacher/agent.py", "TeacherAgent.evaluate_answer")]) == "judge"
    assert infer_source([_Step("core/skills/experience/reflector.py", "Reflector.reflect")]) == "reflect"
    assert infer_source([_Step("unknown/module.py", "f")]) == "other"
    assert infer_source([]) == "other"
    # 从内向外匹配：内层不认识时继续看外层，而不是直接放弃
    assert infer_source([
        _Step("agents/teacher/agent.py", "TeacherAgent.answer"),
        _Step("unknown/module.py", "f"),
    ]) == "teach", "内层 unknown 不匹配时应回退到外层的答疑链路"
    assert infer_source([
        _Step("unknown/module.py", "f"),
        _Step("core/intent/classifier.py", "IntentLlmClassifier.classify"),
    ]) == "intent"


# ---------------------------------------------------------------- 日期范围
def test_resolve_range_rules() -> None:
    """显式日期优先、乱序交换、天数钳制、跨度防呆。"""
    start, end = resolve_range(days=7)
    assert end == _day() and start == _day(-6)

    start, end = resolve_range(since=_day(-2), until=_day(-1))
    assert (start, end) == (_day(-2), _day(-1))

    # 传反了要交换，而不是返回空区间
    assert resolve_range(since=_day(), until=_day(-3)) == (_day(-3), _day())

    # 跨度过大：收窄到「以 until 结尾的最近 90 天」
    start, end = resolve_range(since=_day(-500), until=_day())
    assert parse_day(start) == parse_day(_day()) - timedelta(days=MAX_RANGE_DAYS - 1)

    # days 参数同样防呆
    assert resolve_range(days=10_000)[0] == _day(-(MAX_RANGE_DAYS - 1))
    assert len(iter_days(*resolve_range(since="2026-01-01", until="2026-01-05"))) == 5


def test_iter_days_handles_bad_input() -> None:
    assert iter_days("bad", "2026-01-05") == []
    assert iter_days("2026-01-05", "2026-01-01") == []
    assert parse_day("2026/01/05") is None and parse_day("") is None


# ---------------------------------------------------------------- 埋点（端到端）
class _FakeResponse:
    """假 HTTP 响应：直接吐出预置 JSON（不联网、不产生费用）。"""

    status_code = 200

    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class _FakeAsyncClient:
    """假 httpx.AsyncClient：``post`` 返回预置响应。"""

    reply: dict = {}
    posts: list = []

    def __init__(self, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url: str, **kwargs):
        type(self).posts.append({"url": url, **kwargs})
        return _FakeResponse(type(self).reply)


class _FakeStreamCtx:
    def __init__(self, lines: list) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient(_FakeAsyncClient):
    """假流式客户端：``stream`` 返回逐行 SSE。"""

    lines: list = []

    def stream(self, method: str, url: str, **kwargs):
        return _FakeStreamCtx(list(type(self).lines))


async def test_chat_reports_usage_end_to_end(usage_dir, monkeypatch) -> None:
    """``chat`` 成功路径：响应里的 usage 落盘一行，模型名 / 入口都对得上。"""
    import models.llm as llm_mod
    from core.llm_usage import usage_store

    _FakeAsyncClient.reply = {
        "choices": [{"message": {"content": "你好"}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 8, "total_tokens": 50},
    }
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)

    client = llm_mod.OpenAICompatLLM(api_base="http://fake/v1", api_key="k", model="test-model")
    assert await client.chat([{"role": "user", "content": "hi"}]) == "你好"

    rows = usage_store.read_range(_day(), _day())
    assert len(rows) == 1
    assert rows[0]["model"] == "test-model"
    assert rows[0]["endpoint"] == "chat"
    assert rows[0]["total_tokens"] == 50


async def test_chat_with_tools_reports_usage(usage_dir, monkeypatch) -> None:
    """``chat_with_tools``（ReAct 闭环路径）同样记账。"""
    import models.llm as llm_mod
    from core.llm_usage import usage_store

    _FakeAsyncClient.reply = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)

    client = llm_mod.OpenAICompatLLM(api_base="http://fake/v1", api_key="k", model="m")
    result = await client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert result.content == "ok"

    rows = usage_store.read_range(_day(), _day())
    assert len(rows) == 1 and rows[0]["endpoint"] == "chat_with_tools"


async def test_chat_stream_collects_usage_chunk(usage_dir, monkeypatch) -> None:
    """流式：末尾带 usage 的 chunk 被记账；没有 usage 时静默跳过。"""
    import models.llm as llm_mod
    from core.llm_usage import usage_store

    _FakeStreamClient.lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeStreamClient)
    client = llm_mod.OpenAICompatLLM(api_base="http://fake/v1", api_key="k", model="stream-model")

    chunks = [piece async for piece in client.chat_stream([{"role": "user", "content": "hi"}])]
    assert "".join(chunks) == "你好"

    rows = usage_store.read_range(_day(), _day())
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "chat_stream" and rows[0]["total_tokens"] == 12

    # 厂商没发 usage chunk（默认多数如此）：不记空账、也不报错
    _FakeStreamClient.lines = ['data: {"choices":[{"delta":{"content":"嗨"}}]}', "data: [DONE]"]
    chunks = [piece async for piece in client.chat_stream([{"role": "user", "content": "hi"}])]
    assert "".join(chunks) == "嗨"
    assert len(usage_store.read_range(_day(), _day())) == 1


# ---------------------------------------------------------------- 接口
@pytest.fixture()
def sqlite_db(tmp_path):
    """把数据层切到临时 SQLite 并建表（同 test_auth / test_skill_admin 的约定）。"""
    from db import base as db_base

    db_base.set_database_url(f"sqlite:///{tmp_path / 'llm_usage.db'}")
    db_base.init_db()
    yield
    db_base.set_database_url(f"sqlite:///{tmp_path / 'llm_usage_after.db'}")


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture()
def tokens(sqlite_db, monkeypatch):
    """预置演示账号并登录拿到 admin / student 两枚 token。"""
    monkeypatch.setattr(settings, "auth_enable", True)
    from core.auth.service import seed_demo_users

    seed_demo_users()
    client = _client()
    admin = client.post("/api/auth/login", json={"username": "admin", "password": "123456"}).json()
    stu = client.post("/api/auth/login", json={"username": "student", "password": "123456"}).json()
    return {"admin": admin["token"], "student": stu["token"]}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_usage_api_requires_developer(tokens, usage_dir) -> None:
    """与技能看板同一门禁：未登录 401 / 非 admin 403 / admin 放行。"""
    client = _client()
    assert client.get("/api/admin/llm/usage").status_code == 401
    assert client.get("/api/admin/llm/usage", headers=_h(tokens["student"])).status_code == 403
    assert client.get("/api/admin/llm/usage", headers=_h(tokens["admin"])).status_code == 200


def test_usage_api_filters_by_date(tokens, usage_dir) -> None:
    """日期筛选：只统计区间内的记录；区间外的记录不能混进来。"""
    from core.llm_usage import usage_store

    today, yesterday = _day(), _day(-1)
    usage_store.record({"day": yesterday, "model": "m", "source": "teach",
                        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
    usage_store.record({"day": today, "model": "m", "source": "teach",
                        "prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220})

    client = _client()
    data = client.get(
        "/api/admin/llm/usage",
        params={"since": today, "until": today},
        headers=_h(tokens["admin"]),
    ).json()
    assert data["ok"] and data["operator"] == "admin"
    assert data["range"] == {"since": today, "until": today, "days": 1}
    assert data["totals"] == {"calls": 1, "prompt_tokens": 200, "completion_tokens": 20,
                              "total_tokens": 220, "cached_tokens": 0}
    assert data["config"]["enabled"] is True
    assert data["by_source"][0]["label"] == "答疑讲解"

    # 不传日期 = 最近 7 天（两条都该在）
    data = client.get("/api/admin/llm/usage", headers=_h(tokens["admin"])).json()
    assert data["totals"]["calls"] == 2 and data["range"]["days"] == 7


def test_usage_api_clamps_span(tokens, usage_dir) -> None:
    """跨度防呆：恶意传一个超大区间，接口自动收窄而不是去扫几年的文件。"""
    client = _client()
    data = client.get(
        "/api/admin/llm/usage",
        params={"since": "2000-01-01", "until": _day(), "days": 99999},
        headers=_h(tokens["admin"]),
    ).json()
    assert data["range"]["days"] <= MAX_RANGE_DAYS


# ---------------------------------------------------------------- 前端契约
def test_skill_admin_token_usage_lives_inside_loop_board() -> None:
    """前端结构契约：token 用量**并入「自我更新看板」页面内**（不再单开页签）。"""
    from pathlib import Path

    page = Path(__file__).resolve().parents[1] / "clients" / "skill_admin" / "index.html"
    html = page.read_text(encoding="utf-8")
    for marker in (
        "loop-usage", "/api/admin/llm/usage",
        "u-since", "u-until", "usage-chart", "usage-kpis", "usage-models", "usage-sources",
    ):
        assert marker in html, f"看板内的 token 用量区块缺少：{marker}"
    # 已并入看板：独立的页签 / 面板不得回流
    for gone in ("view-usage", "usage-pane", "usage-scroll"):
        assert gone not in html, f"token 用量应并入看板，不应再有独立的 {gone}"
    # 看板「立即刷新」同时刷新用量（只留一个刷新入口）
    assert "loadLoop(); loadUsage();" in html
