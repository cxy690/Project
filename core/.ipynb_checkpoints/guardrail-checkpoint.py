"""教学护栏与内容安全（G5）：面向未成年人的大班直播课硬性约束。

覆盖 LIVE_CLASS_PIVOT_PLAN.md 第四章 G5 与第十章合规要求：
* **范围护栏**：回答限定在本学科、本学段；超纲问题礼貌引导回课程主线；
* **内容安全**：全量输出经过敏感内容过滤（真实环境应接入内容安全服务）；
* **AI 生成内容标识**：下行事件统一附加水印字段，满足《生成式人工智能服务管理暂行办法》。

设计要点：与 :class:`core.validator.ResponseValidator` 分工——validator 校验「单条回复的
格式与答案泄露」，guardrail 校验「教学范围合规 + 内容安全 + 合规标识」，两者互补。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from config.constants import AI_CONTENT_WATERMARK_TEXT
from config.logging import get_logger
from config.settings import settings

log = get_logger("core.guardrail")

# 极简敏感词库（真实环境应替换为内容安全服务/词库）
_UNSAFE_PATTERNS = [r"去死", r"自杀", r"笨蛋", r"蠢货", r"滚", r"色情", r"暴力", r"赌博"]

# 超纲引导话术：礼貌地把问题拉回课程主线
_REDIRECT_SPEECH = (
    "这个问题很有意思，不过它超出了我们这节课的范围。"
    "我们先把今天的重点弄扎实，课后再一起探讨它，好吗？"
)


@dataclass
class GuardrailVerdict:
    """护栏判定结果。"""

    allowed: bool = True
    in_scope: bool = True                 # 是否落在本学科本学段范围内
    redirect_speech: str = ""             # 超纲时的引导话术
    unsafe: list[str] = field(default_factory=list)  # 命中的不安全词

    def block(self, reason: str) -> None:
        self.allowed = False
        self.unsafe.append(reason)


class TeachingGuardrail:
    """教学护栏：范围限定 + 内容安全过滤 + AI 内容标识。"""

    def __init__(
        self,
        *,
        subject: str | None = None,
        grade: str | None = None,
        enable: bool | None = None,
        safety_enable: bool | None = None,
    ) -> None:
        self.subject = (settings.guardrail_subject if subject is None else subject).strip()
        self.grade = (settings.guardrail_grade if grade is None else grade).strip()
        self.enable = settings.guardrail_enable if enable is None else enable
        self.safety_enable = (
            settings.content_safety_enable if safety_enable is None else safety_enable
        )

    # ------------------------------------------------------------------ 范围护栏
    def screen_subject(self, text: str, subject: str) -> GuardrailVerdict:
        """学科范围护栏：学生提问点名了其它学科时，礼貌引导回本学科。

        检测规则（低误伤优先）见 :func:`config.subjects.detect_other_subject`——
        只有当问题**明确指向其它学科**且不涉及本学科关键词时才拦截；
        「这道题怎么解」这类不含学科词的正常提问不会被误拦。
        """
        verdict = GuardrailVerdict()
        if not self.enable:
            return verdict
        subject = (subject or "").strip()
        if not subject or subject == "综合":      # 未选学科（综合课）不做范围限制
            return verdict
        from config.subjects import detect_other_subject  # 局部导入避免循环依赖

        other = detect_other_subject(text or "", subject)
        if other:
            verdict.in_scope = False
            verdict.redirect_speech = (
                f"我们这节课是{subject}课哦，{other}的问题可以先记在小本本上，"
                f"下节{other}课再请老师讲，好吗？现在我们把{subject}的这道题弄明白～"
            )
            log.info("question out of subject scope ({} -> {}): {}", subject, other, (text or "")[:30])
        return verdict

    def screen_question(self, text: str, *, topic_keywords: list[str] | None = None) -> GuardrailVerdict:
        """判定学生提问是否落在本节课范围内。

        ``topic_keywords`` 为本节课/本学科的关键词；命中任一即视为在范围内。
        未提供关键词时不做范围限制（避免误伤），仅返回 in_scope=True。
        """
        verdict = GuardrailVerdict()
        if not self.enable:
            return verdict
        question = (text or "").strip()
        if not question:
            return verdict
        if topic_keywords:
            if not any(kw and kw in question for kw in topic_keywords):
                verdict.in_scope = False
                verdict.redirect_speech = _REDIRECT_SPEECH
                log.info("question out of scope, redirect: {}", question[:30])
        return verdict

    # ------------------------------------------------------------------ 内容安全
    def filter_output(self, text: str) -> tuple[str, GuardrailVerdict]:
        """对 AI 输出做内容安全过滤，返回 (安全文本, 判定)。

        命中不安全词时以「*」遮蔽，保证面向未成年人的输出零漏放。
        """
        verdict = GuardrailVerdict()
        if not self.safety_enable:
            return text, verdict
        safe = text or ""
        for pat in _UNSAFE_PATTERNS:
            match = re.search(pat, safe)
            if match:
                verdict.block(f"unsafe:{pat}")
                safe = re.sub(pat, lambda m: "*" * len(m.group(0)), safe)
        return safe, verdict

    # ------------------------------------------------------------------ 合规标识
    def tag_ai_content(self, payload: dict) -> dict:
        """给下行事件附加 AI 生成内容标识（合规要求）。"""
        if not settings.ai_content_watermark_enable:
            return payload
        tagged = dict(payload)
        tagged["ai_generated"] = True
        tagged["watermark"] = AI_CONTENT_WATERMARK_TEXT
        return tagged
