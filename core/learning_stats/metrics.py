"""学情指标计算（纯函数，不读盘、不依赖会话）。

口径全部写在这里，便于对账与单测。四个核心问题的回答方式：

1. **学生是否真的完成了学习** → ``completion_score``（完课度）+ ``authenticity_score``
   （真实性）。前者看"时长、环节、收尾检测"三件事做没做，后者看"是不是挂机"。
2. **互动率** → ``interaction_rate`` = 学生发言轮次 / 老师提问次数；
   另给 ``initiative_rate``（主动发起占比）区分"被点名才说话"与"自己想问"。
3. **一节课完成了多少** → ``effective_seconds / planned_seconds``（有效时长覆盖率）
   + ``phases_done / phases_planned``（环节覆盖率），两者都进 ``completion_score``。
4. **如何评估** → 每个分数都由可核对的事件算出来（见各函数注释与
   ``data/learning/lessons/<lesson>.jsonl`` 原始事件），算法改动可对历史课重算。

评分权重（满分 100）：

* 完课度 = 时长覆盖 35 + 环节覆盖 35 + 收尾检测 20 + 出勤稳定 10
* 掌握度 = 首答正确率 50 + 含订正正确率 30 + 出题覆盖 20
* 综合 = 完课度 35 + 掌握度 35 + 互动率 20 + 真实性 10
"""
from __future__ import annotations

import statistics
from typing import Iterable, Sequence

from config.stages import stage_from_grade
from core.learning_stats.models import (
    MASTERY_THRESHOLDS,
    EventKind,
    LessonEvent,
    LessonMetrics,
    MasteryLevel,
)

#: 两个事件之间超过该秒数，判定学生已离场（不计入有效学习时长）。
#: 取 10 分钟而不是 3 分钟：老师连续讲解、学生安静听讲是正常课堂形态，
#: 事件流里这 10 分钟不会有问答记录；只有「超过 10 分钟完全没有师生互动」才算离场。
IDLE_GAP_SEC = 600.0
#: 一节课期望的出题数（低于此数按比例折算"出题覆盖"）
EXPECTED_QUIZ = 3
#: 首答过快阈值（秒）：难题 2 秒内答对，计入"可疑作答"
SUSPICIOUS_FAST_SEC = 2.0
#: 完课度各项权重
W_TIME, W_PHASE, W_WRAP, W_ATTEND = 35.0, 35.0, 20.0, 10.0


def effective_time(events: Sequence[LessonEvent]) -> tuple[float, float, int]:
    """按事件时间轴算 ``(有效秒数, 离场秒数, 离场次数)``。

    判定规则：相邻事件间隔超过 :data:`IDLE_GAP_SEC`，这段空档算离场 ——
    "人不在"的 40 分钟不该算成学习了 40 分钟。这是完课度不虚高的关键。
    """
    stamps = sorted(float(e.ts) for e in events)
    if len(stamps) < 2:
        return 0.0, 0.0, 0
    effective = 0.0
    idle = 0.0
    idle_count = 0
    for prev, cur in zip(stamps, stamps[1:]):
        gap = cur - prev
        if gap <= 0:
            continue
        if gap > IDLE_GAP_SEC:
            idle += gap
            idle_count += 1
        else:
            effective += gap
    return effective, idle, idle_count


def _median(values: Iterable[float]) -> float:
    data = [float(v) for v in values]
    return float(statistics.median(data)) if data else 0.0


def mastery_level_of(score: float) -> str:
    """掌握度分数 -> 分级文案。"""
    for lower, level in MASTERY_THRESHOLDS:
        if score >= lower:
            return level.value
    return MasteryLevel.NOT_REACHED.value


def _pct(numerator: float, denominator: float) -> float:
    return 0.0 if denominator <= 0 else max(0.0, numerator / denominator)


def compute_metrics(
    *,
    lesson_id: str,
    student_id: str,
    subject: str = "综合",
    stage: str = "",
    grade: str = "",
    events: Sequence[LessonEvent],
    planned_seconds: int = 0,
    planned_phases: int = 2,
) -> LessonMetrics:
    """从事件流算出整节课的学情指标。"""
    ordered = sorted(events, key=lambda e: float(e.ts))
    metrics = LessonMetrics(
        lesson_id=lesson_id,
        student_id=student_id,
        subject=subject or "综合",
        stage=stage or stage_from_grade(grade),
        grade=grade,
        planned_seconds=int(planned_seconds or 0),
    )
    metrics.phases_planned = max(1, int(planned_phases or 1))
    if not ordered:
        metrics.mastery_level = mastery_level_of(0.0)
        metrics.flags.append("本节课没有采集到任何事件")
        return metrics

    metrics.started_at = float(ordered[0].ts)
    metrics.ended_at = float(ordered[-1].ts)
    metrics.wall_seconds = int(max(0.0, metrics.ended_at - metrics.started_at))
    effective, idle, idle_count = effective_time(ordered)
    metrics.effective_seconds = int(effective)
    metrics.idle_seconds = int(idle)
    metrics.idle_count = idle_count

    phases: set[str] = set()
    latencies: list[float] = []
    quiz_attempts: dict[str, int] = {}
    total_answers = 0          # 作答次数（同一题答两次算两次）
    fast_correct = 0

    for event in ordered:
        kind = event.kind
        data = event.data or {}
        if kind == EventKind.PHASE.value:
            phase = str(data.get("phase") or "").strip()
            if phase:
                phases.add(phase)
        elif kind == EventKind.TEACHER_QUESTION.value:
            metrics.teacher_questions += 1
        elif kind == EventKind.STUDENT_TEXT.value:
            metrics.student_turns += 1
            if data.get("initiative"):
                metrics.student_initiations += 1
            latency = data.get("latency")
            if isinstance(latency, (int, float)) and latency >= 0:
                latencies.append(float(latency))
        elif kind == EventKind.INTERRUPT.value:
            metrics.interrupts += 1
        elif kind == EventKind.QUIZ_ASKED.value:
            metrics.quiz_total += 1
        elif kind == EventKind.QUIZ_ANSWERED.value:
            qid = str(data.get("question_id") or data.get("question") or "").strip()
            if not qid:
                qid = f"q{metrics.quiz_total}"
            attempt = int(quiz_attempts.get(qid, 0)) + 1
            quiz_attempts[qid] = attempt
            total_answers += 1
            correct = bool(data.get("correct"))
            answer_latency = data.get("latency")
            suspicious = (
                correct
                and isinstance(answer_latency, (int, float))
                and 0 <= float(answer_latency) < SUSPICIOUS_FAST_SEC
            )
            if suspicious:
                fast_correct += 1
            if correct:
                metrics.quiz_correct += 1
                if attempt == 1:
                    metrics.quiz_first_correct += 1

    metrics.phases_done = len(phases)
    metrics.response_median_sec = round(_median(latencies), 2)

    # ---- 完课度 ----------------------------------------------------------
    time_rate = min(1.0, _pct(metrics.effective_seconds, metrics.planned_seconds)) \
        if metrics.planned_seconds else 0.0
    phase_rate = min(1.0, _pct(metrics.phases_done, metrics.phases_planned))
    wrap_window = metrics.wall_seconds * 0.25
    wrap_start = metrics.ended_at - wrap_window if metrics.wall_seconds else metrics.ended_at
    metrics.wrap_done = any(
        e.kind in (EventKind.QUIZ_ANSWERED.value, EventKind.PHASE.value)
        and float(e.ts) >= wrap_start
        and (e.kind != EventKind.PHASE.value or str(e.data.get("phase") or "") in {"summary", "interaction"})
        for e in ordered
    )
    attend_rate = 1.0 if metrics.wall_seconds <= 0 else max(
        0.0, 1.0 - idle_count / max(1, idle_count + max(1, int(metrics.wall_seconds // 600)))
    )
    metrics.completion_score = round(
        W_TIME * time_rate + W_PHASE * phase_rate
        + W_WRAP * (1.0 if metrics.wrap_done else 0.0)
        + W_ATTEND * attend_rate,
        1,
    )

    # ---- 互动 ------------------------------------------------------------
    metrics.interaction_rate = round(
        min(1.0, _pct(metrics.student_turns, max(metrics.teacher_questions, 1))), 3
    )
    metrics.initiative_rate = round(_pct(metrics.student_initiations, metrics.student_turns), 3)

    # ---- 正确率与掌握度 ---------------------------------------------------
    # accuracy：按「作答次数」算（一题先错后对 = 1/2），反映真实答题表现；
    # first_accuracy：按「题数」算（只看第一次），反映听完讲解的即时掌握。
    answered = max(1, len(quiz_attempts))
    metrics.accuracy = round(_pct(metrics.quiz_correct, max(1, total_answers)), 3)
    metrics.first_accuracy = round(_pct(metrics.quiz_first_correct, answered), 3)
    coverage = min(1.0, _pct(len(quiz_attempts), EXPECTED_QUIZ))
    metrics.mastery_score = round(
        100.0 * (0.5 * metrics.first_accuracy + 0.3 * metrics.accuracy + 0.2 * coverage), 1
    )
    metrics.mastery_level = mastery_level_of(metrics.mastery_score)

    # ---- 真实性 ----------------------------------------------------------
    authenticity = 100.0
    if idle_count >= 1:
        authenticity -= min(40.0, 8.0 * idle_count)
        metrics.flags.append(f"课堂中离场 {idle_count} 次，累计 {round(idle / 60)} 分钟")
    if idle >= 300:
        metrics.flags.append("存在 5 分钟以上的静默空档，可能挂机")
    answered_total = max(1, len(quiz_attempts))
    fast_ratio = fast_correct / answered_total
    if fast_ratio >= 0.3:
        authenticity -= 15.0 if fast_ratio < 0.6 else 25.0
        metrics.flags.append(f"有 {fast_correct} 道题在 2 秒内答对，建议核实是否照抄答案")
    if metrics.planned_seconds and time_rate < 0.3:
        authenticity -= 15.0
        metrics.flags.append("有效学习时长不足计划的三成")
    if metrics.student_turns <= 1 and metrics.wall_seconds >= 600:
        authenticity -= 10.0
        metrics.flags.append("整节课学生几乎没有发言")
    metrics.authenticity_score = round(max(0.0, min(100.0, authenticity)), 1)

    # ---- 综合 ------------------------------------------------------------
    metrics.overall_score = round(
        0.35 * metrics.completion_score
        + 0.35 * metrics.mastery_score
        + 0.20 * 100.0 * metrics.interaction_rate
        + 0.10 * metrics.authenticity_score,
        1,
    )
    if metrics.interaction_rate < 0.3 and metrics.teacher_questions >= 2:
        metrics.flags.append("互动率偏低：老师提问多、学生回应少")
    if not metrics.wrap_done:
        metrics.flags.append("未完成收尾检测（课程后段没有作答或总结）")
    return metrics


def summarize_lessons(items: Sequence[LessonMetrics]) -> dict[str, float]:
    """把多节课汇总成学生画像（算术平均，空列表返回全 0）。"""
    if not items:
        return {
            "lessons": 0, "completion": 0.0, "mastery": 0.0,
            "interaction": 0.0, "authenticity": 0.0, "overall": 0.0,
            "total_minutes": 0.0,
        }
    count = len(items)
    return {
        "lessons": count,
        "completion": round(sum(i.completion_score for i in items) / count, 1),
        "mastery": round(sum(i.mastery_score for i in items) / count, 1),
        "interaction": round(sum(i.interaction_rate for i in items) / count, 3),
        "authenticity": round(sum(i.authenticity_score for i in items) / count, 1),
        "overall": round(sum(i.overall_score for i in items) / count, 1),
        "total_minutes": round(sum(i.effective_seconds for i in items) / 60.0, 1),
    }
