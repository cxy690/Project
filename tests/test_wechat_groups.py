"""群推送测试：群路由、班级群隐私口径、防重复推送、周区间。

不真的调微信接口：把 ``notifier.get_client`` 换成假客户端，只验证
「发到哪个群、发了什么」，以及「班级群里绝不能出现单个学生的分数」。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from core.learning_stats.models import LessonMetrics
from wechat import groups, handlers, notifier


class FakeClient:
    """记录发送内容的假客户端（群 webhook / 单聊共用）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_group_text(self, text: str, webhook: str = "") -> bool:
        self.sent.append((webhook, text))
        return True

    async def send_text(self, open_id: str, text: str) -> bool:
        self.sent.append((open_id, text))
        return True


def _metrics(student_id: str = "stu-1", lesson_id: str = "L1", **overrides) -> LessonMetrics:
    base = {
        "lesson_id": lesson_id,
        "student_id": student_id,
        "subject": "数学",
        "grade": "四年级",
        "started_at": 1_700_000_000.0,
        "planned_seconds": 2400,
        "effective_seconds": 1800,
        "completion_score": 88.0,
        "mastery_score": 82.0,
        "mastery_level": "应用",
        "interaction_rate": 0.5,
        "quiz_total": 6,
        "quiz_first_correct": 5,
        "flags": ["讲解后即时练习还没跟上"],
    }
    base.update(overrides)
    return LessonMetrics(**base)


GROUP_CONFIG = {
    "groups": [
        {
            "id": "g_xiaoming",
            "name": "小明家长群",
            "webhook": "https://example.com/hook/parent",
            "kind": "parent",
            "student_ids": ["stu-1"],
        },
        {
            "id": "g_class_3a",
            "name": "初三A班家长群",
            "webhook": "https://example.com/hook/class",
            "kind": "class",
            "student_ids": ["stu-1", "stu-2", "stu-3"],
        },
    ]
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离群配置 / 绑定 / 去重文件，并换掉微信客户端。"""
    cfg_path = tmp_path / "groups.json"
    cfg_path.write_text(json.dumps(GROUP_CONFIG, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(groups, "GROUPS_PATH", cfg_path)
    monkeypatch.setattr(handlers, "BINDINGS_PATH", tmp_path / "bindings.json")
    monkeypatch.setattr(groups, "student_label", lambda sid, group=None: "小明")
    fake = FakeClient()
    monkeypatch.setattr(notifier, "get_client", lambda: fake)
    return fake


# ---------------------------------------------------------------- 配置解析
def test_load_groups_rejects_bad_webhook(tmp_path, monkeypatch) -> None:
    path = tmp_path / "groups.json"
    path.write_text(json.dumps({"groups": [
        {"id": "bad", "webhook": "not-a-url", "kind": "class"},
        {"id": "ok", "webhook": "https://example.com/hook", "kind": "weird", "student_ids": ["s1"]},
    ]}), encoding="utf-8")
    monkeypatch.setattr(groups, "GROUPS_PATH", path)

    loaded = groups.load_groups()
    assert [g.id for g in loaded] == ["ok"]
    # 未知 kind 一律按班级群（更保守：不发明细）
    assert loaded[0].is_class


def test_groups_for_student_filters_by_flag(tmp_path, monkeypatch) -> None:
    path = tmp_path / "groups.json"
    path.write_text(json.dumps({"groups": [
        {"id": "a", "webhook": "https://e.com/a", "kind": "class", "student_ids": ["s1"], "lesson_end": False},
        {"id": "b", "webhook": "https://e.com/b", "kind": "class", "student_ids": ["s1"]},
        {"id": "c", "webhook": "https://e.com/c", "kind": "parent", "student_ids": ["s2"]},
    ]}), encoding="utf-8")
    monkeypatch.setattr(groups, "GROUPS_PATH", path)

    assert [g.id for g in groups.groups_for_student("s1", for_lesson=True)] == ["b"]
    assert [g.id for g in groups.groups_for_student("s1")] == ["a", "b"]
    assert groups.groups_for_student("s2", kind="parent")[0].id == "c"


# ---------------------------------------------------------------- 隐私口径
def test_class_notice_has_no_scores() -> None:
    text = handlers.lesson_notice_text(_metrics(), label="小明")
    for word in ("完课度", "掌握度", "互动率", "分"):
        assert word not in text, f"班级群提示不应出现 {word}"
    assert "私聊" in text


def test_class_weekly_hides_average_for_small_sample() -> None:
    two = [_metrics(student_id="stu-1"), _metrics(student_id="stu-2", lesson_id="L2")]
    text = handlers.class_weekly_text("初三A班", "（10-14 ~ 10-20）", two, min_students=3)
    assert "平均完课度" not in text
    assert "暂不展示班级平均分" in text

    three = two + [_metrics(student_id="stu-3", lesson_id="L3")]
    full = handlers.class_weekly_text("初三A班", "（10-14 ~ 10-20）", three, min_students=3)
    assert "平均完课度" in full
    # 概览只给聚合值，不点名
    for sid in ("stu-1", "stu-2", "stu-3"):
        assert sid not in full


# ---------------------------------------------------------------- 路由 + 防重
async def test_lesson_push_routes_by_group_kind_and_dedups(env) -> None:
    metrics = _metrics()

    sent = await notifier.push_lesson_report(metrics)
    assert sent == 2  # 一对一群 + 班级群（家长未绑定，私聊为 0）

    hooks = [hook for hook, _ in env.sent]
    assert "https://example.com/hook/parent" in hooks
    assert "https://example.com/hook/class" in hooks

    parent_text = next(t for h, t in env.sent if h.endswith("/parent"))
    class_text = next(t for h, t in env.sent if h.endswith("/class"))
    assert "掌握度" in parent_text and "82" in parent_text
    assert "掌握度" not in class_text and "82" not in class_text

    # 断线重连会重复结算同一节课：第二次必须被去重挡住
    assert await notifier.push_lesson_report(metrics) == 0
    assert len(env.sent) == 2


async def test_short_lesson_is_skipped(env) -> None:
    metrics = _metrics(lesson_id="L-short", effective_seconds=60)
    assert await notifier.push_lesson_report(metrics) == 0
    assert env.sent == []


# ---------------------------------------------------------------- 周报
def test_week_range_starts_on_monday() -> None:
    tuesday = datetime(2026, 10, 20, 19, 0).timestamp()
    since, until, label = notifier.week_range(tuesday)
    start = datetime.fromtimestamp(since)
    assert start.weekday() == 0
    assert (start.month, start.day, start.hour, start.minute) == (10, 19, 0, 0)
    assert until >= tuesday
    assert "10-19" in label


async def test_weekly_all_class_group_gets_summary_only(env, monkeypatch) -> None:
    """周报：班级群一条聚合概览，一对一家校群只发该生明细。"""
    # 三个学生同一周各一节课：用假的分组读取避开真实 data/learning
    lessons = {
        "stu-1": [_metrics(lesson_id="W1")],
        "stu-2": [_metrics(student_id="stu-2", lesson_id="W2")],
        "stu-3": [_metrics(student_id="stu-3", lesson_id="W3")],
    }
    monkeypatch.setattr(notifier, "_lessons_by_student", lambda: lessons)
    # 明细文案由 handlers.weekly_text 自己的测试覆盖，这里只验证路由
    monkeypatch.setattr(
        handlers, "weekly_text",
        lambda sid, **kw: f"【本周学习汇总】{sid}\n完课度 88 分｜掌握度 82 分",
    )

    # week_start 指定为学生课次所在那一周（2023-11-14 周二），避免依赖当前日期
    results = await notifier.push_weekly_all(week_start=1_700_000_000, force=True)
    by_group = {row["group"]: row for row in results}
    assert by_group["g_class_3a"]["lessons"] == 3
    assert "平均完课度" in by_group["g_class_3a"]["text"]

    class_sends = [t for h, t in env.sent if h.endswith("/class")]
    assert len(class_sends) == 1  # 班级群一周只发一条，不是一人一条
    assert "stu-1" not in class_sends[0]

    parent_sends = [t for h, t in env.sent if h.endswith("/parent")]
    assert parent_sends and "掌握度" in parent_sends[0]
