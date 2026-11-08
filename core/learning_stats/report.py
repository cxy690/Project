"""课后学情报告：把指标翻译成老师 / 家长看得懂的话。

**为什么先做规则版、不直接用 LLM**：
报告里的每个结论都必须能追到一条指标或一条事件（"互动率 0.32，因为老师提问 8 次、
学生只回应 3 次"）。LLM 生成的漂亮话没法对账，还可能放大负面评价。
这里用固定规则产出「结论 + 依据」，需要润色时再由上层可选地过一遍 LLM。
"""
from __future__ import annotations

from typing import Sequence

from core.learning_stats.models import LessonEvent, LessonMetrics, LessonReport

#: 各分项的合格线（低于则进「待改进」）
GOOD_COMPLETION = 75.0
GOOD_MASTERY = 75.0
GOOD_INTERACTION = 0.5
GOOD_AUTHENTICITY = 80.0


def _is_low(metrics: LessonMetrics) -> bool:
    return metrics.overall_score < 60.0


def headline_of(metrics: LessonMetrics) -> str:
    """一句话结论（按综合分的档位，不堆形容词）。"""
    score = metrics.overall_score
    if metrics.authenticity_score < GOOD_AUTHENTICITY:
        return "这节课有较长时间没有互动，先要确认学生是否真的在场学习"
    if score >= 85:
        return "这节课完成度高，学生参与主动，掌握情况良好"
    if score >= 75:
        return "这节课基本完成，个别环节还能再扎实一些"
    if score >= 60:
        return "这节课完成了主线，但参与度或掌握度有明显短板"
    return "这节课完成度偏低，需要先确认学生是否真的在学"


def _highlights(metrics: LessonMetrics) -> list[str]:
    items: list[str] = []
    if metrics.completion_score >= GOOD_COMPLETION:
        items.append(
            f"完课度 {metrics.completion_score} 分：有效学习 {metrics.effective_seconds // 60} 分钟，"
            f"覆盖 {metrics.phases_done}/{metrics.phases_planned} 个环节"
        )
    if metrics.mastery_score >= GOOD_MASTERY:
        items.append(
            f"掌握度 {metrics.mastery_score} 分（{metrics.mastery_level}）："
            f"首答正确 {metrics.quiz_first_correct} 题、共作答 {metrics.quiz_total} 题"
        )
    if metrics.interaction_rate >= GOOD_INTERACTION:
        items.append(
            f"互动率 {metrics.interaction_rate:.2f}：老师提问 {metrics.teacher_questions} 次，"
            f"学生回应 {metrics.student_turns} 次"
        )
    if metrics.initiative_rate >= 0.4:
        items.append(f"主动提问占比 {metrics.initiative_rate:.2f}，不是「问一句答一句」")
    if metrics.authenticity_score >= 95:
        items.append("学习真实性 100 分：全程无明显离场或异常作答")
    return items[:3]


def _improvements(metrics: LessonMetrics) -> list[str]:
    items: list[str] = []
    if metrics.completion_score < GOOD_COMPLETION:
        if metrics.planned_seconds and metrics.effective_seconds < metrics.planned_seconds * 0.6:
            items.append(
                f"有效学习时长只有 {metrics.effective_seconds // 60} 分钟"
                f"（计划 {metrics.planned_seconds // 60} 分钟）"
            )
        if metrics.phases_done < metrics.phases_planned:
            items.append(f"只走了 {metrics.phases_done}/{metrics.phases_planned} 个教学环节")
        if not metrics.wrap_done:
            items.append("没有完成收尾检测，无法确认这节课的目标是否达成")
    if metrics.mastery_score < GOOD_MASTERY:
        if metrics.quiz_total == 0:
            items.append("整节课没有出题，掌握度无法评估")
        elif metrics.first_accuracy < 0.6:
            items.append(
                f"首答正确率只有 {metrics.first_accuracy:.0%}，"
                "说明讲解后即时练习还没跟上"
            )
    if metrics.interaction_rate < GOOD_INTERACTION and metrics.teacher_questions >= 2:
        items.append(
            f"互动率 {metrics.interaction_rate:.2f}：老师提问 {metrics.teacher_questions} 次、"
            f"回应 {metrics.student_turns} 次，学生偏被动"
        )
    if metrics.authenticity_score < GOOD_AUTHENTICITY:
        items.append(f"学习真实性 {metrics.authenticity_score} 分，存在挂机或异常作答迹象")
    return items[:4]


def _next_actions(metrics: LessonMetrics) -> list[str]:
    actions: list[str] = []
    if metrics.mastery_score < 70:
        actions.append("下一节课先用 3 道同类型小题复测本节知识点，再进入新内容")
    if metrics.interaction_rate < 0.5:
        actions.append("把大问题拆成 2—3 个小问题，每讲完一步就让学生用自己的话说一遍")
    if metrics.authenticity_score < 80:
        actions.append("下次课开场明确约定「我在听就回一句」，并安排中途一次复述")
    if metrics.first_accuracy < 0.5 and metrics.quiz_total:
        actions.append("讲解时增加「例题示范 + 半放手练习」环节，避免一讲到底")
    if not metrics.wrap_done:
        actions.append("课程最后 5 分钟固定做一次小结检测（一道题或一次复述）")
    return actions[:3] or ["保持当前节奏，下节课继续观察掌握度变化"]


def _parent_note(metrics: LessonMetrics) -> str:
    minutes = max(1, metrics.effective_seconds // 60)
    level = metrics.mastery_level or "待评估"
    if metrics.authenticity_score < GOOD_AUTHENTICITY:
        return (
            f"本节有效学习约 {minutes} 分钟，但课堂中有较长时间没有互动，"
            f"建议确认孩子上课时是否在专心。知识点掌握评估为「{level}」，"
            "可以让孩子复述一遍本节内容再判断是否需要补课。"
        )
    if _is_low(metrics):
        return (
            f"本节有效学习约 {minutes} 分钟，课堂完成度偏低。"
            f"掌握度评估为「{level}」，建议陪孩子复述一下本节内容，并确认上课时是否能安静坐住。"
        )
    if metrics.interaction_rate < GOOD_INTERACTION:
        return (
            f"本节有效学习约 {minutes} 分钟，知识点掌握评估为「{level}」。"
            "孩子听得多、说得少，建议在家多让他讲一遍今天学了什么。"
        )
    return (
        f"本节有效学习约 {minutes} 分钟，完课度 {metrics.completion_score} 分、"
        f"掌握度 {metrics.mastery_score} 分（{level}），课堂参与正常，继续按当前节奏学习即可。"
    )


def build_report(
    metrics: LessonMetrics, events: Sequence[LessonEvent] | None = None
) -> LessonReport:
    """生成课后报告（规则版，指标可对账）。"""
    return LessonReport(
        lesson_id=metrics.lesson_id,
        student_id=metrics.student_id,
        headline=headline_of(metrics),
        highlights=_highlights(metrics),
        improvements=_improvements(metrics),
        next_actions=_next_actions(metrics),
        parent_note=_parent_note(metrics),
        metrics=metrics,
    )
