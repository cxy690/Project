"""微信消息处理：家长绑定、学情查询、课表、帮助。

**只做三件事**，不把业务塞进这里：
1. 解析家长的消息（纯文本关键词）；
2. 调 ``core/learning_stats`` 取学情；
3. 返回一段可直接发出去的文本。

绑定关系落 ``data/wechat/bindings.json``（openid -> 学生），
删除走 :func:`unbind`，符合「未成年人数据可撤回」的要求。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Sequence

from config.logging import get_logger
from wechat.config import WeChatConfig, load_config

log = get_logger("wechat.handlers")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BINDINGS_PATH = PROJECT_ROOT / "data" / "wechat" / "bindings.json"

HELP_TEXT = """【学习助手】可用指令：
· 学情 —— 查看孩子最近一节课的学情
· 学情 全部 —— 查看最近 5 节
· 课表 —— 查看剩余课时
· 绑定 学生ID 验证码 —— 绑定孩子（验证码找班主任要）
· 解绑 —— 取消绑定
· 帮助 —— 再看一遍这份说明"""

#: 家长发图片 / 语音 / 文件时的回复（当前只处理文本，别让人以为没反应）
MEDIA_REPLY = "暂时只能看懂文字消息。发送「帮助」查看可用指令。"


# ---------------------------------------------------------------- 绑定存储
def _load_bindings() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(BINDINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 文件不存在/损坏都按空处理
        return {}


def _save_bindings(data: dict[str, dict[str, Any]]) -> None:
    BINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BINDINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(BINDINGS_PATH)


def bound_student(open_id: str) -> str:
    """该微信用户绑定的学生 id（未绑定返回空串）。"""
    return str((_load_bindings().get(open_id) or {}).get("student_id") or "")


def open_ids_of(student_id: str) -> list[str]:
    """反查：绑定了该学生的所有微信 open_id（学情通知用）。"""
    return [
        open_id
        for open_id, item in _load_bindings().items()
        if str(item.get("student_id") or "") == str(student_id)
    ]


def bind(open_id: str, student_id: str) -> bool:
    if not open_id or not student_id:
        return False
    data = _load_bindings()
    data[open_id] = {"student_id": student_id, "bound_at": time.time()}
    _save_bindings(data)
    return True


def unbind(open_id: str) -> bool:
    data = _load_bindings()
    if open_id not in data:
        return False
    data.pop(open_id, None)
    _save_bindings(data)
    return True


# ---------------------------------------------------------------- 文本工具
def _fmt_pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _fmt_score(value: float) -> str:
    """分数去掉无意义的小数位：``88.0`` → ``88``，``91.7`` 保持 ``91.7``。

    指标层用 ``round(x, 1)`` 存，直接插值会让家长看到「完课度：88.0 分」，
    像没做完的模板。整分不显示小数、非整分保留一位即可。
    """
    return f"{float(value):g}"


def lesson_text(metrics: Any) -> str:
    """把一节课的指标排版成一条微信文本（家长可读）。"""
    minutes = max(1, int(metrics.effective_seconds) // 60)
    lines = [
        f"【本节学情】{metrics.subject} · {metrics.grade or metrics.stage or '未填年级'}",
        f"有效学习：{minutes} 分钟（计划 {max(1, metrics.planned_seconds // 60)} 分钟）",
        f"完课度：{_fmt_score(metrics.completion_score)} 分｜"
        f"互动率：{_fmt_pct(metrics.interaction_rate)}",
        f"掌握度：{_fmt_score(metrics.mastery_score)} 分（{metrics.mastery_level}）",
    ]
    if metrics.quiz_total:
        lines.append(
            f"练习：{metrics.quiz_total} 题，首答正确 {metrics.quiz_first_correct} 题"
        )
    if metrics.flags:
        lines.append("需要关注：")
        lines.extend(f"· {flag}" for flag in metrics.flags[:3])
    return "\n".join(lines)


def weekly_text(
    student_id: str,
    *,
    since: float = 0.0,
    until: float = 0.0,
    limit: int = 5,
    label: str = "",
) -> str:
    """学生周报：默认近 5 节；传 ``since`` / ``until`` 时按自然周区间。

    为什么区间模式不截断 5 节：一周上 6 节课是常态，截断会让周报"少算几节"，
    家长一对照课表就发现对不上。区间模式取该区间内**全部**课次。
    """
    from core.learning_stats import list_lessons, summarize_lessons  # noqa: PLC0415

    lessons = list_lessons(student_id=student_id, limit=200 if (since or until) else limit)
    if since or until:
        lessons = [
            item
            for item in lessons
            if (not since or float(item.started_at) >= since)
            and (not until or float(item.started_at) <= until)
        ]
    else:
        lessons = lessons[:limit]
    if not lessons:
        return "最近没有学情记录，等上完一节课再看看。"
    summary = summarize_lessons(lessons)
    head = "【本周学习汇总】" + (label or "")
    lines = [
        f"{head}共 {summary['lessons']} 节，有效学习 {summary['total_minutes']:.0f} 分钟",
        f"完课度 {_fmt_score(summary['completion'])} 分｜"
        f"掌握度 {_fmt_score(summary['mastery'])} 分｜"
        f"互动率 {_fmt_pct(summary['interaction'])}",
    ]
    weak = [flag for item in lessons for flag in (item.flags or [])]
    if weak:
        lines.append("待关注：")
        lines.extend(f"· {flag}" for flag in list(dict.fromkeys(weak))[:3])
    return "\n".join(lines)


def lesson_notice_text(metrics: Any, *, label: str = "", show_name: bool = True) -> str:
    """班级群课后提示：**只通知「已生成」，不出现任何分数或指标**。

    班级群里有其他家长，单个学生的完课度 / 掌握度 / 待改进项属于未成年人数据，
    不能外发。明细只走家长私聊（``lesson_text``）。
    """
    subject = str(getattr(metrics, "subject", "") or "").strip()
    who = f"{label}同学" if (show_name and label) else "有同学"
    tail = f"（{subject}）" if subject else ""
    return (
        f"【课堂完成】{who}的本节课{tail}已结束，学情报告已生成。\n"
        "家长可在微信里发送「学情」私聊查看；本节明细不在群里公开。"
    )


def class_weekly_text(
    group_name: str,
    label: str,
    lessons: Sequence[Any],
    *,
    min_students: int = 3,
) -> str:
    """班级群周报概览：只给聚合值，**不点名、不给单个学生分数**。

    ``min_students`` 是隐私下限：班里只有 1—2 个学生上课时，"平均分"等于个人分数，
    会被家长一眼反推出来。样本不够就只报节数与时长，不报平均分。
    """
    from core.learning_stats import summarize_lessons  # noqa: PLC0415

    title = f"【班级周学情】{group_name or '本班'}" + (label or "")
    if not lessons:
        return title + "\n本周暂无上课记录。"
    students = {
        str(getattr(item, "student_id", "") or "")
        for item in lessons
        if str(getattr(item, "student_id", "") or "")
    }
    summary = summarize_lessons(list(lessons))
    lines = [
        title,
        f"本周 {len(students)} 名学生上课，共 {summary['lessons']} 节，"
        f"累计有效学习 {summary['total_minutes']:.0f} 分钟",
    ]
    if len(students) >= max(1, int(min_students)):
        lines.append(
            f"平均完课度 {summary['completion']:.0f} 分｜掌握度 {summary['mastery']:.0f} 分｜"
            f"互动率 {_fmt_pct(summary['interaction'])}"
        )
    else:
        lines.append("本周上课人数较少，暂不展示班级平均分。")
    counts: dict[str, int] = {}
    for item in lessons:
        for flag in getattr(item, "flags", []) or []:
            counts[flag] = counts.get(flag, 0) + 1
    if counts:
        top = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
        lines.append("本周共性问题：")
        lines.extend(f"· {flag}（{count} 节）" for flag, count in top)
    lines.append("个别学生的明细已单独发送给家长，群里不公开。")
    return "\n".join(lines)


def _course_text() -> str:
    """剩余课时（读 ``data/courses.json``，与课程计费同一份事实源）。"""
    try:
        raw = json.loads((PROJECT_ROOT / "data" / "courses.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "暂时查不到课时信息，请联系班主任。"
    courses = raw.get("courses") if isinstance(raw, dict) else raw
    if not isinstance(courses, list) or not courses:
        return "暂时没有进行中的课程包。"
    latest = courses[-1]
    return (
        f"【课时】当前课程包：{latest.get('subject') or '综合'}；"
        f"已用 {int(latest.get('elapsed_seconds') or 0) // 60} 分钟；"
        f"已续费 {int(latest.get('renew_count') or 0)} 次。"
    )


def handle_text(open_id: str, text: str, config: WeChatConfig | None = None) -> str:
    """处理一条家长文本，返回要回复的内容（空串表示不回复）。"""
    cfg = config or load_config()
    content = (text or "").strip()
    if not content:
        return ""
    lowered = content.lower()

    if lowered in {"帮助", "help", "?", "？"}:
        return HELP_TEXT

    if content.startswith("绑定"):
        if not cfg.bind_enabled:
            return "绑定功能未开启，请联系班主任。"
        parts = content.split()
        if len(parts) < 3:
            return "格式：绑定 学生ID 验证码"
        student_id, secret = parts[1].strip(), parts[2].strip()
        if secret != cfg.bind_secret:
            return "验证码不正确，请向班主任确认。"
        bind(open_id, student_id)
        return f"绑定成功，之后发送「学情」即可查看 {student_id} 的学习情况。"

    if content.startswith("解绑"):
        return "已解绑。" if unbind(open_id) else "你还没有绑定学生。"

    if content.startswith("学情"):
        student_id = bound_student(open_id)
        if not student_id:
            return "还没有绑定学生。请发送：绑定 学生ID 验证码"
        from core.learning_stats import list_lessons  # noqa: PLC0415

        if "全部" in content or "最近" in content:
            lessons = list_lessons(student_id=student_id, limit=5)
            if not lessons:
                return "还没有学情记录。"
            return "\n\n".join(lesson_text(item) for item in lessons)
        lessons = list_lessons(student_id=student_id, limit=1)
        if not lessons:
            return "还没有学情记录，上完一节课后再来查看。"
        return lesson_text(lessons[0])

    if content.startswith("课表") or content.startswith("课时"):
        return _course_text()

    return "没看懂这条消息。发送「帮助」查看可用指令。"


def welcome_text(open_id: str, config: WeChatConfig | None = None) -> str:
    """家长进入客服会话时的欢迎语：绑定过就直接把最近一节课的学情端上来。

    微信客服的 48 小时窗口限制让「课后主动推」不可靠，但 ``enter_session``
    事件不受影响 —— 家长只要点开窗口，就能零操作看到最新学情。
    """
    cfg = config or load_config()
    student_id = bound_student(open_id)
    if not student_id:
        return "你好！我是学习助手。\n" + HELP_TEXT

    from core.learning_stats import list_lessons  # noqa: PLC0415

    lessons = list_lessons(student_id=student_id, limit=1)
    if not lessons:
        return "你好！还没有学情记录，上完一节课后再来查看。\n" + HELP_TEXT
    return lesson_text(lessons[0]) + "\n\n发送「学情 全部」查看最近 5 节。"


def handle_event(event: str, open_id: str) -> str:
    """处理关注 / 入群等事件。"""
    if event in {"subscribe", "enter_agent"}:
        return "你好！我是学习助手。发送「帮助」查看如何绑定孩子、查询学情。"
    return ""
