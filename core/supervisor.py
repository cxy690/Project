"""AI 教师总控（Supervisor）：把路由、答疑、麦控串成完整会话闭环。

一次会话一个 Supervisor 实例，持有本会话的：
* 学生端代理（麦控 + 设备状态）；
* AI 教师（讲解 + 白板）；
* 意图路由 / 回复校验 / 题目解析；
* 可选作业模式与知识检索。

对外只暴露 ``handle_turn`` / ``on_vad`` 等事件入口。
"""
from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from typing import Any

from agents.student.agent import StudentAgent
from agents.teacher.agent import TeacherAgent
from config.constants import ClassPhase, IntentType, is_valid_mood
from config.logging import get_logger
from config.settings import settings
from config.teachers import teacher_for_subject
from core import course_session, math_solver
from core.audio_utils import append_tail_silence
from core.avatar_stream import save_avatar_video
from core.graph import Node, TaskGraph
from core.guardrail import TeachingGuardrail
from config.subjects import resolve_subject
from core.hint_homework import HintLevel
from core.langgraph_flow import LANGGRAPH_AVAILABLE, run_answer_graph
from core.router import IntentRouter
from core.state.models import (
    AIResponse,
    ChatMessage,
    StudentTurn,
)
from core.state.session import SessionState
from core.turn_graph import run_turn_graph
from core.validator import ResponseValidator
from core.viseme import build_viseme_timeline
from document_parsing.pipeline import QuestionParser
from models.asr import get_asr
from models.avatar import get_avatar
from models.tts import get_tts

log = get_logger("core.supervisor")

# 问候词：待机问候命中这些词时，数字人用“打招呼片段”作底片对口型
_GREET_WORDS = ("你好", "您好", "哈喽", "哈啰", "嗨", "大家好", "同学们好", "早上好", "中午好", "晚上好")

# 开关指向 langgraph 但依赖缺失时，仅告警一次并回退自研图
_langgraph_warned = False


def _warn_langgraph_missing() -> None:
    global _langgraph_warned
    if not _langgraph_warned:
        log.warning("ORCHESTRATOR=langgraph but langgraph is not installed; fallback to builtin TaskGraph")
        _langgraph_warned = True


class Supervisor:
    """单会话 AI 教师总控。"""

    def __init__(
        self,
        session: SessionState,
        *,
        student: StudentAgent | None = None,
        teacher: TeacherAgent | None = None,
        router: IntentRouter | None = None,
        validator: ResponseValidator | None = None,
        parser: QuestionParser | None = None,
        retriever: Any | None = None,
    ) -> None:
        self.session = session
        self.student = student or StudentAgent()
        self.teacher = teacher or TeacherAgent()
        self.router = router or IntentRouter()
        self.validator = validator or ResponseValidator()
        self.parser = parser or QuestionParser()
        self.guardrail = TeachingGuardrail()    # 教学护栏：学科范围 + 内容安全
        self.retriever = retriever
        self._lg_graph: Any | None = None       # LangGraph 答疑子图编译结果（懒加载缓存）
        self._turn_graph: Any | None = None     # LangGraph 回合主图编译结果
        self._idle_nudges = 0                   # 空闲问候已触发次数
        self.asr = get_asr()
        self.tts = get_tts()
        self.avatar = get_avatar()

    # ================================================================ 生命周期
    async def on_start(self) -> None:
        await self.student.on_session_start(self.session)
        self.session.push_status(ai_running=True)

    async def on_idle(self) -> AIResponse | None:
        """长时间无交互时，AI 主动打招呼并**围绕刚才的问题**询问（温柔、不打扰）。"""
        if (
            not settings.idle_nudge_enable
            or self.session.ai_speaking
            or self.session.teaching
            or self._idle_nudges >= settings.idle_nudge_max
        ):
            return None
        self._idle_nudges += 1
        speech = await self._idle_speech_async()
        # 命中“你好”类问候词 -> 下发给数字人用“打招呼片段”对口型（其余时候用讲解底片）
        meta = {"gesture": "greet"} if any(w in speech for w in _GREET_WORDS) else {}
        return await self._speak_async(AIResponse(speech=speech, meta=meta))

    def _idle_speech(self) -> str:
        """生成主动问候话术：优先“对应上 AI 刚才问的问题”。"""
        # 1) 正在等学生作答的小测验/追问 -> 直接追问那题
        pq = self.session.pending_question or {}
        q = str(pq.get("question") or "").strip()
        if q:
            return f"同学，你还在屏幕前吗？刚才那道题——{q} 想到答案了吗？慢慢来，想好了告诉我就好。"
        # 2) 最近一条 AI 提问 -> 围绕它追问
        last_q = self._last_ai_question()
        if last_q:
            return f"同学，你还在屏幕前吗？关于「{last_q}」，有想法了吗？也可以直接说「继续」哦。"
        # 3) 无上下文 -> 温柔问候
        prompts = [
            "你好同学，你还在屏幕前吗？有不懂的随时叫我哦。",
            "我在呢，需要我继续讲哪道题吗？",
            "同学，休息一下也没关系，想继续时说一声就好～",
        ]
        return prompts[min(self._idle_nudges - 1, len(prompts) - 1)]

    def _idle_context(self) -> str:
        """空闲问候可围绕的“上一问题”上下文（优先待作答的小测验）。"""
        pq = self.session.pending_question or {}
        q = str(pq.get("question") or "").strip()
        return q or self._last_ai_question()

    async def _idle_speech_async(self) -> str:
        """空闲问候话术：优先用大模型生成（可能含“你好”->触发打招呼动作），失败回退模板。"""
        ctx = self._idle_context()
        try:
            from models.llm import get_llm  # 局部导入避免循环依赖

            prompt = (
                "你是耐心温柔的小学 AI 老师。学生已经静默了一小会儿。"
                "请用一句不超过30字的中文，自然地询问学生是否还在，可适度使用‘你好’等问候语；"
                "只输出这一句话，不要加引号或解释。"
            )
            if ctx:
                prompt += f" 可在句中顺带追问：{ctx}。"
            raw = await get_llm().chat([{"role": "user", "content": prompt}], temperature=0.8, max_tokens=60)
            text = (raw or "").strip().strip("“”\"' \n")
            if text and len(text) <= 60:
                return text
        except Exception as err:  # noqa: BLE001 - 失败回退模板
            log.warning("idle llm greet failed: {}", err)
        return self._idle_speech()

    def _last_ai_question(self) -> str:
        """取最近一条“像问题”的 AI 消息（用于空闲追问问对题）。"""
        for msg in reversed(self.session.history):
            if getattr(msg, "role", "") != "ai" or not getattr(msg, "text", ""):
                continue
            t = msg.text.strip()
            if t.endswith(("？", "?")) or ("吗" in t and len(t) <= 40):
                return t[:40]
        return ""

    # ================================================================ 语音
    def on_vad(self, has_voice: bool, ts_ms: int) -> None:
        """VAD 帧驱动麦控状态机（学生说话 -> AI 静默）。"""
        self.student.on_vad(has_voice, ts_ms, self.session)

    async def on_voice(self, audio: bytes) -> AIResponse | None:
        """收到一段学生语音：转写 -> 若已静默则触发答疑。"""
        if not self.student.microphone_on:
            self.session.add_message(
                ChatMessage(role="ai", text="麦克风好像关着，先点一下「打开麦克风」再提问哦。")
            )
            return None
        try:
            transcript = await self.asr.transcribe(audio)
        except Exception as err:  # noqa: BLE001 - ASR 异常不能静默，否则前端一直停在「识别中…」
            log.exception("asr transcribe failed: {}", err)
            transcript = ""
        transcript = (transcript or "").strip()
        log.info("asr transcript: {!r} (audio {} bytes)", transcript, len(audio))
        if not transcript:
            # 识别为空（静音 / 太短 / 网络抖动）也要给可见反馈，避免前端「识别中…」卡住
            msg = "我这边没有听清，可以再靠近麦克风说一遍吗？也可以直接打字告诉我～"
            self.session.add_message(ChatMessage(role="ai", text=msg))
            self.session.push_event("ai_speech", {"text": msg, "mood": "thinking"})
            return None
        turn = self.student.build_turn_from_voice(transcript)
        return await self.handle_turn(turn)

    # ================================================================ 主导入
    async def handle_turn(self, turn: StudentTurn) -> AIResponse | None:
        """处理一次学生输入（按 ORCHESTRATOR 分派到 LangGraph 主图或内建实现）。"""
        self._idle_nudges = 0  # 学生一有输入，空闲问候计数归零
        if settings.orchestrator == "langgraph":
            if LANGGRAPH_AVAILABLE:
                return await run_turn_graph(self, turn)
            _warn_langgraph_missing()
        return await self._handle_turn_builtin(turn)

    async def _handle_turn_builtin(self, turn: StudentTurn) -> AIResponse | None:
        """内建（TaskGraph）回合处理，与 LangGraph 主图行为保持一致。"""
        # 优先：正等待作答且本次输入像作答 -> 走判题而非新答疑
        if self._should_treat_as_answer(turn):
            turn.intent = IntentType.TEXT_QUESTION
            self.session.add_message(ChatMessage(role="student", text=turn.text, image_id=turn.image_id))
            return await self._turn_answer_check(turn)

        intent = await self.router.route_async(turn)
        turn.intent = intent
        self.session.add_message(ChatMessage(role="student", text=turn.text, image_id=turn.image_id))

        if intent is IntentType.CONTROL:
            self._handle_control(turn.text)
            return None

        if intent is IntentType.CHITCHAT:
            return await self._turn_chitchat()

        return await self._turn_teach(turn, intent)

    async def _turn_chitchat(self) -> AIResponse:
        """闲聊回合：温柔回应，不进入答疑链路。"""
        return await self._speak_async(AIResponse(speech="我在呢，我们把不会的题发给我，我来帮你讲～"))

    async def _turn_teach(self, turn: StudentTurn, intent: IntentType, *, ocr_text: str = "") -> AIResponse:
        """答疑回合：标记教学中（体态提醒让路）-> 跑答疑链路 -> 播报 -> 视情况追问。"""
        # 学科范围护栏：问题明确指向其它学科时，礼貌引导回本学科，不进答疑链路
        # （作答回合不拦：学生回答「A」这类选项不含学科词，也无需范围判断）
        verdict = self.guardrail.screen_subject(turn.text or "", self.session.subject)
        if not verdict.in_scope:
            self.session.add_message(ChatMessage(role="student", text=turn.text, image_id=turn.image_id))
            return await self._speak_async(AIResponse(speech=verdict.redirect_speech))
        self.session.pending_question = None      # 新答疑：丢弃上一题待答追问
        self.session.followup_asked = 0
        self.session.teaching = True
        self.session.ai_speaking = True
        self.session.push_status(ai_running=True)
        try:
            use_streaming = (
                settings.digital_human_enable
                and self.avatar.produces_video
                and intent is not IntentType.IMAGE_QUESTION
            )
            hint_level = self._resolve_hint_level(intent)
            board_first_mode = (settings.board_first_mode or "off").strip().lower()
            if board_first_mode != "off":
                # 白板先行：先写「题意理解」，再结合它给出最终解答（与流式链路互斥）
                response = await self._turn_teach_board_first(
                    turn, intent, hint_level=hint_level, ocr_text=ocr_text,
                    mode=board_first_mode)
            elif use_streaming:
                response = await self._turn_teach_streaming(
                    turn, intent, hint_level=hint_level, ocr_text=ocr_text)
            else:
                response = await self._run_answer_pipeline(
                    turn, intent, ocr_text=ocr_text, hint_level=hint_level)
                await self._speak_async(response)
            # 记录本题知识点，供辅导课「随机出题」使用
            self.session.last_topic = str(response.meta.get("title") or turn.text or "").strip()[:40]
            if self.session.course_active and settings.course_quiz_enable:
                # 辅导课模式：讲解完自动出一道模拟题并等待作答
                await self.on_quiz_request(self.session.last_topic)
            else:
                self._ask_followup(response)
            return response
        finally:
            self.session.teaching = False
            self.session.ai_speaking = False
            self.session.push_status(ai_running=False)

    # ================================================================ 理解先行（先说理解 -> 再上解答）
    async def _turn_teach_board_first(
        self, turn: StudentTurn, intent: IntentType, *,
        hint_level: HintLevel | None, ocr_text: str = "", mode: str = "single_call",
    ) -> AIResponse:
        """理解先行分派：``single_call``（方案 B，单次调用两段）/ ``two_call``（方案 A，两次调用）。"""
        if mode == "two_call":
            return await self._board_first_two_call(
                turn, intent, hint_level=hint_level, ocr_text=ocr_text)
        return await self._board_first_single_call(
            turn, intent, hint_level=hint_level, ocr_text=ocr_text)

    @staticmethod
    def _understanding_text(resp: AIResponse) -> str:
        """把「理解」产物还原成文本，供阶段二作为上下文回流给老师。"""
        parts: list[str] = []
        title = str(resp.meta.get("title") or "").strip()
        if title:
            parts.append(title)
        parts.extend(f"· {p}" for p in (resp.meta.get("understanding") or []))
        steps = resp.meta.get("understand_steps") or []
        parts.extend(f"{i}. {s}" for i, s in enumerate(steps, 1))
        return "\n".join(parts)

    def _remember_understanding(self, resp: AIResponse) -> str:
        """记下本回合的「理解」文本（便于追问/复盘复用），并打一条日志。"""
        text = self._understanding_text(resp)
        self.session.scratch["understanding_text"] = text
        log.info("理解先行：已用语音讲出理解：{}", text.replace("\n", " | ")[:120])
        return text

    async def _board_first_single_call(
        self, turn: StudentTurn, intent: IntentType, *,
        hint_level: HintLevel | None, ocr_text: str = "",
    ) -> AIResponse:
        """方案 B：单次 LLM 产出「理解 + 解答」两段。

        理解段**只用语音讲**（不写白板），白板只下发结合后的解答；
        相比两次调用少一次 LLM 往返，且理解与解答同源不跑偏。
        """
        notes = await self._board_notes_async(turn, intent, ocr_text)
        spoken_understanding = False
        understand_task: asyncio.Task[AIResponse] | None = None
        final: AIResponse | None = None
        async for stage, resp in self.teacher.stream_board_first(
            turn, self.session, hint_level=hint_level, context_notes=notes,
        ):
            if stage == "understanding":
                self._remember_understanding(resp)
                spoken_understanding = True
                # 理解一出来就让老师先讲（后台任务），与「解答仍在流式生成」并行，首响更早
                understand_task = asyncio.create_task(self._speak_async(resp))
            else:
                final = resp

        if final is None:
            final = AIResponse(speech="这道题我们再想想看。")
        if intent is IntentType.IMAGE_QUESTION and turn.image_id:
            # 图片题：圈画题目区域（白板上只叠解答，圈画保留）
            final.board_ops = [
                self.teacher.board.annotate_region(
                    0.08, 0.08, 0.84, 0.30, image_id=turn.image_id, label="题目区域")
            ] + list(final.board_ops)
        self._finalize_streamed_response(final, hint_level)

        # ★ 白板先上：LLM 一出结果就推给学生看。
        # 语音（克隆 TTS + 数字人逐句渲染）要数秒到十几秒，绝不能占着首屏时间——
        # 否则观感就是「问了半天白板才出来」。
        if final.board_ops:
            self.session.push_board(final.board_ops)

        # 先等「理解」讲完，再讲解答，保证两段语音不重叠
        if understand_task is not None:
            try:
                await understand_task
            except Exception as err:  # noqa: BLE001 - 理解播报失败不应阻断解答
                log.warning("understanding speech failed: {}", err)
        final.meta["understand_spoken"] = spoken_understanding
        final.meta["board_first"] = "single_call"
        await self._speak_async(final, push_board=False)   # 白板已下发，只补文本与语音
        return final

    async def _board_first_two_call(
        self, turn: StudentTurn, intent: IntentType, *,
        hint_level: HintLevel | None, ocr_text: str = "",
    ) -> AIResponse:
        """方案 A：两次 LLM 调用 —— 先用语音讲「理解」，再把理解回流生成结合后的解答上板。"""
        # ---- 阶段一：老师先用语音讲出「理解」（不上板）----
        draft = await self.teacher.draft_understanding(turn, self.session)
        understand_text = self._remember_understanding(draft)
        await self._speak_async(draft)

        # ---- 阶段二：把「理解」作为上下文回流给老师，生成结合后的解答 ----
        notes = await self._board_notes_async(turn, intent, ocr_text)
        combined_note = (
            "【你刚在语音里讲过的题意理解】\n"
            + (understand_text or "（无）")
            + "\n请在此基础上继续推导，给出完整、正确的解答与最终答案；"
            "不要重复照抄上面的理解文字，直接给出结论与关键步骤。"
        )
        if intent is IntentType.IMAGE_QUESTION:
            response = await self.teacher.answer_image(
                turn, self.session, ocr_text=ocr_text, hint_level=hint_level,
                extra_notes=[combined_note],
            )
        else:
            response = await self.teacher.answer(
                turn, self.session, hint_level=hint_level,
                context_notes=notes + [combined_note],
            )
        self._finalize_streamed_response(response, hint_level)

        # ---- 阶段三：白板只下发「解答」----
        response.meta["understand_spoken"] = True
        response.meta["board_first"] = "two_call"
        await self._speak_async(response)
        return response

    async def _board_notes_async(
        self, turn: StudentTurn, intent: IntentType, ocr_text: str
    ) -> list[str]:
        """异步取答疑前上下文：RAG 检索（失败忽略）+ 图片题 OCR 说明。"""
        notes: list[str] = []
        if self.retriever is not None and turn.text:
            try:
                notes = list(await self.retriever.retrieve(turn.text) or [])
            except Exception as err:  # noqa: BLE001 - 检索失败不影响答疑
                log.warning("retriever failed: {}", err)
        if intent is IntentType.IMAGE_QUESTION and ocr_text:
            notes = [f"题目OCR识别内容：{ocr_text}"] + notes
        return notes

    # ================================================================ 互动追问
    def _ask_followup(self, response: AIResponse) -> None:
        """把 AI 产物里的 followup 设为待答问题，并推 ai_ask 让前端渲染作答卡片。"""
        followup = response.meta.get("followup")
        if not settings.interaction_enable or not isinstance(followup, dict):
            return
        question = str(followup.get("question") or "").strip()
        if not question or self.session.followup_asked >= settings.interaction_max_followups:
            return
        options = [str(o).strip() for o in (followup.get("options") or []) if str(o).strip()]
        self.session.pending_question = {
            "question": question,
            "expected": str(followup.get("answer") or "").strip(),
            "hint": str(followup.get("hint") or "").strip(),
            "options": options,
            "attempts": 0,
            "source_title": str(response.meta.get("title") or ""),
        }
        self.session.followup_asked += 1
        self.session.push_event(
            "ai_ask",
            {"question": question, "options": options, "index": self.session.followup_asked},
        )

    def _should_treat_as_answer(self, turn: StudentTurn) -> bool:
        """有待答追问、且本次输入不像“新问题”时，视为对追问的作答。"""
        if self.session.pending_question is None:
            return False
        text = (turn.text or "").strip()
        if not text or turn.image_id:
            return False
        if self.router.route(turn) is IntentType.CONTROL:
            return False
        # 模拟题/追问的选项作答：与选项原文精确一致即判定为作答
        # （选项可能含标点且较长，需豁免下面的长度与标点规则）
        pq = self.session.pending_question or {}
        options = [str(o).strip() for o in (pq.get("options") or [])]
        if options and text in options:
            return True
        # 像新问题（含疑问特征）则不当作答，视为新的答疑
        if self.router._looks_like_question(text):  # noqa: SLF001 - 复用同一判定规则
            return False
        # 过长或含句读的，多半是新的提问而非一个答案，按新答疑处理
        if len(text) > 30 or any(ch in text for ch in "，,。.；;、"):
            return False
        return True

    async def _turn_answer_check(self, turn: StudentTurn) -> AIResponse:
        """判定学生对追问的作答，给出反馈与提示（错误可重试，超限公布答案）。"""
        pq = self.session.pending_question or {}
        question = str(pq.get("question") or "")
        expected = str(pq.get("expected") or "")
        self.session.teaching = True
        self.session.ai_speaking = True
        self.session.push_status(ai_running=True)
        try:
            verdict = await self.teacher.evaluate_answer(
                question, expected, turn.text, options=pq.get("options")
            )
            correct = bool(verdict.get("correct"))
            feedback = str(verdict.get("feedback") or "").strip()
            hint = str(verdict.get("hint") or "").strip() or str(pq.get("hint") or "").strip()
            allow_retry = False
            if correct:
                speech = feedback or "答对啦，真棒！"
                self.session.pending_question = None
            else:
                attempts = int(pq.get("attempts") or 0) + 1
                allow_retry = attempts < settings.interaction_max_retries
                if allow_retry:
                    speech = (feedback or "再想想看～") + (f" {hint}" if hint else "")
                    pq["attempts"] = attempts
                    self.session.pending_question = pq
                else:
                    reveal = f"没关系，正确答案是：{expected}。" if expected else "没关系，我们下次再练。"
                    speech = (feedback or "再想想看～") + " " + reveal
                    self.session.pending_question = None
            if correct or not allow_retry:
                self.session.quiz = None      # 答对或不再重试：收起当前模拟题
            # 数字人手势 + 形象状态编排：答对点头并表扬、答错摇头（可重试）或摊手
            gesture = "nod" if correct else ("shake" if allow_retry else "shrug")
            mood = "praising" if correct else "thinking"
            response = AIResponse(speech=speech, meta={"gesture": gesture, "mood": mood})
            await self._speak_async(response)
            self.session.push_event(
                "ai_check",
                {"correct": correct, "feedback": speech, "expected": expected, "allow_retry": allow_retry},
            )
            return response
        finally:
            self.session.teaching = False
            self.session.ai_speaking = False
            self.session.push_status(ai_running=False)

    # ================================================================ 1v1 辅导课
    async def on_course_start(self, subject: str | None = None) -> AIResponse | None:
        """进入辅导页：记录学科与起点，教师主动打招呼（语言 + 挥手动作）。

        阶段推进由前端计时器驱动，后端只用 ``course_session`` 做时间映射，
        不引入长驻定时器，避免与会话循环的 tick 二次调度冲突。
        """
        if subject:
            self.session.subject = resolve_subject(subject)
            # 同步写入 scratch：teacher 的提示词从 scratch 读学科（历史上无人写入，
            # 导致提示词永远显示「综合」，讲解/出题被 LLM 默认成数学）
            self.session.scratch["subject"] = self.session.subject
        self.session.course_active = True
        self.session.course_started_at = time.time()
        self.session.course_deadline = self.session.course_started_at + course_session.duration_seconds()
        self.session.course_phase = ClassPhase.OPENING.value
        self.session.quiz_count = 0

        self.session.ai_speaking = True
        self.session.push_status(ai_running=True)
        try:
            response = self.teacher.compose_course_greeting(self.session.subject)
            await self._speak_async(response)
        finally:
            self.session.ai_speaking = False
            self.session.push_status(ai_running=False)
        self.session.push_event("course_state", self._course_state())
        return response

    def _course_state(self) -> dict[str, Any]:
        """当前课程状态（前端据此更新学科 / 阶段 / 计时显示）。"""
        return {
            "subject": self.session.subject,
            "active": self.session.course_active,
            "phase": self.session.course_phase,
            "phase_label": course_session.phase_label(self.session.course_phase),
            "duration_seconds": course_session.duration_seconds(),
            "started_at": self.session.course_started_at,
            "deadline": self.session.course_deadline,
            "quiz_count": self.session.quiz_count,
        }

    async def on_course_phase(self, phase: str | None) -> AIResponse | None:
        """前端计时器到点上报的阶段切换：播报过渡话术。

        正在讲解/思考时不插话（避免打断），仅更新阶段状态。
        """
        target = course_session.to_phase(phase)
        if target is None:
            return None
        self.session.course_phase = target.value
        if target is ClassPhase.ENDED:
            self.session.course_active = False
        self.session.push_event("course_state", self._course_state())
        if self.session.ai_speaking or self.session.teaching:
            return None
        response = self.teacher.compose_phase_intro(target, self.session.subject)
        if not response.speech:
            return None
        await self._speak_async(response)
        return response

    async def on_quiz_request(self, topic: str = "") -> AIResponse | None:
        """生成一道模拟题并进入「等待作答」状态。

        复用既有追问判题链路：题目写入 ``pending_question``，学生的作答会经
        ``_should_treat_as_answer`` -> ``_turn_answer_check`` 自动判题与反馈。
        """
        if not settings.course_quiz_enable:
            return None
        self.session.teaching = True
        self.session.ai_speaking = True
        self.session.push_status(ai_running=True)
        try:
            quiz = await self.teacher.generate_quiz(
                self.session.subject, topic or self.session.last_topic
            )
            self.session.quiz = quiz.model_dump()
            self.session.quiz_count += 1
            self.session.pending_question = {
                "question": quiz.question,
                "expected": quiz.answer,
                "hint": quiz.hint,
                "options": list(quiz.options),
                "attempts": 0,
                "source_title": quiz.topic,
                "kind": "quiz",
            }
            self.session.followup_asked += 1
            self.session.push_event(
                "ai_quiz",
                {
                    "qid": quiz.qid,
                    "type": quiz.type,
                    "question": quiz.question,
                    "options": list(quiz.options),
                    "index": self.session.quiz_count,
                    "subject": self.session.subject,
                    "topic": quiz.topic,
                },
            )
            response = AIResponse(
                speech=f"我们来做一道模拟题：{quiz.question}",
                meta={"mood": "waiting", "gesture": "point"},
            )
            await self._speak_async(response)
            return response
        finally:
            self.session.teaching = False
            self.session.ai_speaking = False
            self.session.push_status(ai_running=False)

    # ================================================================ 图片
    async def handle_image(self, image_id: str, image_bytes: bytes, text: str = "") -> AIResponse | None:
        """上传题目图片：OCR 解析 + 图片讲解。"""
        parsed = await self.parser.parse(image_bytes)
        self.session.board_images[image_id] = {"ocr": parsed.raw_text, "subject": parsed.subject}
        self.session.scratch["last_parsed"] = parsed
        turn = self.student.build_turn_from_image(image_id, text=text or parsed.content)

        return await self._turn_teach(turn, IntentType.IMAGE_QUESTION, ocr_text=parsed.raw_text)

    # ================================================================ 内部
    def _build_answer_graph(self) -> TaskGraph:
        async def retrieve(ctx: dict[str, Any]) -> dict[str, Any]:
            turn: StudentTurn = ctx["turn"]
            if self.retriever is None:
                return {"notes": []}
            notes = await self.retriever.retrieve(turn.text)
            return {"notes": notes}

        async def generate(ctx: dict[str, Any]) -> dict[str, Any]:
            turn: StudentTurn = ctx["turn"]
            hint_level: HintLevel | None = ctx.get("hint_level")
            if turn.intent is IntentType.IMAGE_QUESTION:
                response = await self.teacher.answer_image(
                    turn, self.session, ocr_text=ctx.get("ocr_text", ""), hint_level=hint_level
                )
            else:
                response = await self.teacher.answer(
                    turn, self.session, hint_level=hint_level, context_notes=ctx.get("notes")
                )
            return {"response": response}

        async def validate(ctx: dict[str, Any]) -> dict[str, Any]:
            response: AIResponse = ctx["response"]
            result = self.validator.validate(response, hint_level=ctx.get("hint_level"))
            if not result.ok:
                log.warning("response validation issues: {}", result.reasons)
                # 校验不通过时降级为纯话术，避免向学生输出异常白板内容
                response.board_ops = []
            return {"validation": result}

        async def solve(ctx: dict[str, Any]) -> dict[str, Any]:
            # 独立求解/验算：用 sympy 校验模型答案，错误则追加更正
            response: AIResponse = ctx["response"]
            # 独立求解/验算：仅存在「含等号的等式」时生效；require_answer 避免把
            # 概念公式（如 ax+b=0）误判为「解不唯一」而污染讲解话术
            math_solver.apply_verification(response, self.teacher.board, require_answer=True)
            return {"response": response}

        return (
            TaskGraph("answer")
            .add(Node("retrieve", retrieve))
            .add(Node("generate", generate, deps=("retrieve",)))
            .add(Node("validate", validate, deps=("generate",)))
            .add(Node("solve", solve, deps=("validate",)))
        )

    async def _run_answer_pipeline(
        self,
        turn: StudentTurn,
        intent: IntentType,
        *,
        ocr_text: str = "",
        hint_level: HintLevel | None = None,
    ) -> AIResponse:
        # hint_level 已由调用方解析时直接复用（避免作业提示层级被重复升级）
        if hint_level is None:
            hint_level = self._resolve_hint_level(intent)
        ctx: dict[str, Any] = {
            "turn": turn,
            "intent": intent,
            "hint_level": hint_level,
            "ocr_text": ocr_text,
            "notes": [],
        }
        if settings.orchestrator == "langgraph":
            if LANGGRAPH_AVAILABLE:
                return await run_answer_graph(self, ctx)
            _warn_langgraph_missing()
        await self._build_answer_graph().run(ctx)
        response: AIResponse = ctx.get("response") or AIResponse(speech="这道题我们再想想看。")
        return response

    # ---------------------------------------------------------------- 低延迟流式答疑
    async def _turn_teach_streaming(
        self, turn: StudentTurn, intent: IntentType, *,
        hint_level: HintLevel | None, ocr_text: str = "",
    ) -> AIResponse:
        """流式答疑：LLM 话术边生成边逐句 TTS/渲染，白板等结构化内容并行生成。

        双路并行（均含同一份 RAG 上下文）：
        * 话术路经 ``sentence_queue`` 逐句到达 -> 立刻送 TTS/数字人（首句延迟 ≈ 首句生成时间）；
        * 结构路 ``teacher.answer`` 完成后再下发白板与聊天文本，并做校验/验算收尾。
        """
        notes: list[str] = []
        if self.retriever is not None and turn.text:
            try:
                notes = await self.retriever.retrieve(turn.text)
            except Exception as err:  # noqa: BLE001 - 检索失败不影响答疑
                log.warning("retriever failed: {}", err)

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        # 结构路：完整答疑管线（RAG -> JSON -> 校验降级 -> 验算）原样保留
        response_task = asyncio.create_task(self._run_answer_pipeline(
            turn, intent, ocr_text=ocr_text, hint_level=hint_level,
        ))
        # 话术路：LLM 流式生成纯讲解正文，逐句入队
        speech_task = asyncio.create_task(self.teacher.speech_stream(
            turn, self.session, hint_level=hint_level, context_notes=notes, sentence_queue=queue,
        ))

        async def _board_ready() -> AIResponse:
            """白板先行：结构路一生成完立刻上白板，不等逐句渲染完成。

            之前白板要等全部句子渲染下发后才推送，导致「老师已经在讲、
            白板却慢好几秒」；现在两路并行，白板与讲解音视频几乎同步出现。
            """
            resp = await response_task
            streamed_text = await speech_task     # 话术已全部生成（渲染仍在后台进行）
            if streamed_text:
                resp.speech = streamed_text
            self._finalize_streamed_response(resp, hint_level)
            if resp.board_ops:
                self.session.push_board(resp.board_ops)
            return resp

        board_task = asyncio.create_task(_board_ready())

        async def _sentences() -> AsyncIterator[str]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        # 过渡句：结构路（白板）要完整跑完 RAG -> 结构化 JSON -> 校验 -> 验算，
        # 通常比话术路的首句晚几秒。先用一句简短开场白顶上，把「提问后的静默」
        # 变成「老师先应一声再开讲」，正式讲解与白板随后自然跟上。
        # 该过渡句的 TTS + 渲染放到后台任务，与「正式讲解首句生成」并行，削减首响延迟：
        # 原本要等 opener 整段渲染完（数秒）才开始消费句子流，现在这段等待与 LLM 首句生成重叠。
        opener_task: asyncio.Task[dict[str, Any] | None] | None = None
        if settings.digital_human_enable and self.avatar.produces_video:
            opener_task = asyncio.create_task(
                self._render_opener_sentence(self._opener_sentence())
            )

        sentences_iter = _sentences()
        if opener_task is not None:
            try:
                opener_result = await opener_task
                if opener_result is not None:
                    self.session.push_event("ai_avatar", opener_result)
            except Exception as err:  # noqa: BLE001 - 过渡句失败不影响正式答疑
                log.warning("opener sentence skipped: {}", err)

        speak_ok = await self._push_avatar_video(sentences_iter)
        response = await board_task
        if speak_ok:
            # 白板已在 _board_ready 先行下发；这里只补聊天文本与教师状态
            self._speak(response, push_board=False)
        else:
            # 渲染不可用：退回「纯语音 + 静态待机画面」。
            # 绝不能什么都不发——否则学生会等半天一点声音都没有（比慢更糟）。
            self._speak(response, push_board=False)
            log.warning("avatar video unavailable; 退回纯语音播报")
            await self._push_audio(response)
            self.session.push_event(
                "chat_message",
                {"role": "ai", "text": "（数字人引擎正在加载，先用语音讲解～）"},
            )
        return response

    _OPENERS: tuple[str, ...] = (
        "好，这道题我们一起来看一下。",
        "嗯，这个问题问得好，我们一步步来分析。",
        "来，先别急，我们把题目读一遍，找出已知条件。",
        "好，我来帮你理一理这道题的思路。",
    )

    def _opener_sentence(self) -> str:
        """过渡开场白：答疑正式开始前先应一声，掩盖结构化输出的生成延迟。"""
        import random

        return random.choice(self._OPENERS)

    async def _render_opener_sentence(self, opener: str) -> dict[str, Any] | None:
        """合成并渲染过渡句；无音频或渲染失败返回 None（静默跳过，不影响正式答疑）。"""
        try:
            opener_audio = await self.tts.synthesize(opener, teacher_id=self._teacher_id())
        except Exception as err:  # noqa: BLE001 - TTS 失败仅跳过过渡句
            log.warning("opener tts failed: {}", err)
            return None
        if not opener_audio:
            return None
        return await self._render_avatar_sentence(opener_audio, opener, None)

    def _finalize_streamed_response(self, response: AIResponse, hint_level: HintLevel | None) -> None:
        """流式答疑收尾：与答疑管线保持一致的校验降级 + sympy 验算。"""
        try:
            result = self.validator.validate(response, hint_level=hint_level)
            if result is not None and not result.ok:
                response.board_ops = []
        except Exception as err:  # noqa: BLE001 - 校验异常不应中断播报
            log.warning("streamed response validation failed: {}", err)
        math_solver.apply_verification(response, self.teacher.board, require_answer=True)

    def _resolve_hint_level(self, intent: IntentType) -> HintLevel | None:
        """作业模式下按题目请求下一层提示。"""
        hw = self.session.scratch.get("homework")
        if hw is None:
            return None
        level, _ = hw.request_hint(escalate=True)
        return level

    def _handle_control(self, text: str) -> None:
        if "关闭摄像头" in text:
            self.student.set_camera(False, self.session)
        elif "打开摄像头" in text:
            self.student.set_camera(True, self.session)
        elif "关闭麦克风" in text:
            self.student.set_microphone(False, self.session)
        elif "打开麦克风" in text:
            self.student.set_microphone(True, self.session)
        elif "清空白板" in text:
            self.session.push_board([self.teacher.board.clear()])

    def _speak(self, response: AIResponse, *, push_board: bool = True) -> AIResponse:
        """把 AI 产物同步到白板、消息与客户端（语音由客户端 TTS 播放）。

        ``push_board=False``：白板已在别处（如流式答疑的 _board_ready）先行下发，
        这里只补聊天文本与教师状态，避免白板指令重复推送。
        """
        if push_board and response.board_ops:
            self.session.push_board(response.board_ops)
        if response.speech:
            self.session.add_message(
                ChatMessage(role="ai", text=response.speech, board_ops=response.board_ops)
            )
            payload: dict[str, Any] = {"text": response.speech}
            mood = response.meta.get("mood")
            if is_valid_mood(mood):
                # 教师形象状态（打招呼/讲解/思考/表扬/等待）；非法值不下发，前端保持原状态
                payload["mood"] = mood
            elif mood:
                log.debug("ignored unknown teacher mood: {}", mood)
            self.session.push_event("ai_speech", payload)
        return response

    async def _speak_async(self, response: AIResponse, *, push_board: bool = True) -> AIResponse:
        """异步播报：先同步白板/文本，再合成真实 TTS 音频下发（数字人口型用）。

        ``push_board=False``：白板已提前单独下发过（避免重复推送与清屏重绘）。
        """
        self._speak(response, push_board=push_board)
        await self._push_audio(response)
        return response

    @staticmethod
    def _spoken_text(response: AIResponse) -> str:
        """播报文本 = 主话术 + 白板分步解析（让学生听到白板上的内容）。"""
        def _clean(value: str) -> str:
            return (value or "").strip().rstrip("。！？!?；;、")

        segments: list[str] = []
        if _clean(response.speech):
            segments.append(_clean(response.speech))
        for i, step in enumerate(response.meta.get("steps") or [], 1):
            text = _clean(str(step))
            if text:
                segments.append(f"第{i}步，{text}")
        followup = response.meta.get("followup") or {}
        question = _clean(str(followup.get("question") or ""))
        if question:
            segments.append(f"我来考考你：{question}")
        return ("。".join(segments) + "。") if segments else ""

    async def _push_audio(self, response: AIResponse) -> None:
        """合成 TTS 音频并下发 ``ai_audio``；不可用时下发 ``ai_speak_text`` 让前端回退朗读。"""
        speech = self._spoken_text(response)
        if not speech:
            return
        if not settings.digital_human_enable:
            self.session.push_event("ai_speak_text", {"text": speech})
            return
        gesture = response.meta.get("gesture")        # 手势基元名（可为 None = 纯讲解）
        if self.avatar.produces_video:
            if await self._push_avatar_video(speech, gesture):
                return
            # 渲染不可用：退回「纯语音 + 静态待机画面」，绝不能静音。
            log.warning("avatar video unavailable; 退回纯语音播报")
            self.session.push_event(
                "chat_message",
                {"role": "ai", "text": "（数字人引擎正在加载，先用语音讲解～）"},
            )
            await self._push_audio(response)
            return
        try:
            audio = await self.tts.synthesize(speech, teacher_id=self._teacher_id())
        except Exception as err:  # noqa: BLE001 - TTS 失败不影响文本播报
            log.warning("tts synthesize failed: {}", err)
            audio = b""
        if not audio:
            # MockTTS / 空音频：让前端退回浏览器朗读
            self.session.push_event("ai_speak_text", {"text": speech})
            return
        payload: dict[str, Any] = {
            "audio": base64.b64encode(audio).decode("ascii"),
            "mime": "audio/mpeg",
            "text": speech,
        }
        if settings.digital_human_viseme_enable:
            visemes = build_viseme_timeline(speech)
            if visemes:
                payload["visemes"] = visemes
        self.session.push_event("ai_audio", payload)

    def _teacher_id(self) -> str:
        """当前会话的教师标识（= 学科 key）；该学科没有独立档案时返回空串。

        空串让渲染引擎沿用默认底片（向后兼容旧的单老师部署）。
        """
        profile = teacher_for_subject(self.session.subject)
        return profile.key if profile else ""

    async def _render_avatar_sentence(
        self, audio_chunk: bytes, sentence: str, gesture: str | None
    ) -> dict[str, Any] | None:
        """渲染单句数字人视频；失败时短暂重试，避免引擎重启/加载中被误判为不可用。

        返回引擎结果（``mode == "video"``）；重试仍失败返回 ``None``。
        """
        teacher_id = self._teacher_id()
        if not teacher_id:
            # 还没确定学科（如刚进页面的打招呼）时引擎没有对应底片，渲染必然 404；
            # 直接跳过，避免「重试 3 次 + 每次等 1.5s」白白拖慢首响。
            log.info("跳过数字人渲染：本回合尚未确定学科（teacher_id 为空）")
            return None

        attempts = max(1, settings.avatar_render_attempts)
        last = ""
        for i in range(attempts):
            try:
                result = await self.avatar.render(
                    audio_chunk, text=sentence, gesture=gesture,
                    teacher_id=teacher_id,
                )
                if result.get("mode") == "video":
                    return result
                last = f"mode={result.get('mode')}"
            except Exception as err:  # noqa: BLE001 - 网络/引擎异常均可重试
                last = str(err)
                if "404" in last:
                    # 底片缺失这类确定性错误：重试无意义，立刻放弃（省掉数秒重试等待）
                    log.warning("avatar render 404（底片缺失），放弃本句渲染：{}", last)
                    return None
            log.warning("avatar render attempt {}/{} failed: {}", i + 1, attempts, last)
            if i + 1 < attempts:
                await asyncio.sleep(1.5)
        return None

    async def _render_avatar_sentence_padded(
        self, audio_chunk: bytes, sentence: str, gesture: str | None
    ) -> dict[str, Any] | None:
        """渲染单句：音频尾部补静音（让末帧闭嘴）+ 视频落盘为 URL（避免大 base64 卡顿）。

        补静音与落盘都是阻塞 IO，放到线程里做，避免卡住事件循环（也与会话循环并行）。
        """
        padded = audio_chunk
        if settings.avatar_tail_silence_seconds > 0:
            padded = await asyncio.to_thread(
                append_tail_silence, audio_chunk, settings.avatar_tail_silence_seconds
            )
        result = await self._render_avatar_sentence(padded, sentence, gesture)
        # 视频落盘 -> stream_url：前端 <video> 按 URL 流式加载，
        # 省掉 1~3MB base64 经 WebSocket 传输后的 JSON.parse/解码卡顿。
        if (
            result is not None
            and settings.avatar_stream_url_enable
            and result.get("mode") == "video"
            and result.get("video")
        ):
            seq = int(self.session.scratch.get("avatar_seq", 0)) + 1
            self.session.scratch["avatar_seq"] = seq
            url = await asyncio.to_thread(
                save_avatar_video,
                str(result["video"]),
                f"{self.session.session_id}_{seq}",
            )
            if url:
                result.pop("video", None)
                result["stream_url"] = url
        return result

    async def _tts_sentence_stream(
        self, sentences: AsyncIterator[str]
    ) -> AsyncIterator[tuple[bytes, str]]:
        """把「句子流」转换为 TTS 的 ``(音频, 句子)`` 流（每句独立合成，随到随合成）。"""
        async for sentence in sentences:
            async for audio_chunk, text in self.tts.synthesize_stream(
                sentence, teacher_id=self._teacher_id()
            ):
                yield audio_chunk, text

    async def _push_avatar_video(
        self, speech: str | AsyncIterator[str], gesture: str | None = None
    ) -> bool:
        """视频数字人链路：流式 TTS 逐句合成 -> 数字人引擎逐句渲染 -> 下发 ``ai_avatar``。

        ``speech`` 可为完整话术字符串（内部按句切分），也可为句子异步迭代器
        （LLM 流式答疑：句子边生成边到达，随到随合成，进一步压低首句延迟）。

        返回 True 表示整段话术均成功下发视频；任一句失败立即返回 False，
        调用方回退到「ai_audio + visemes」照片木偶链路，保证播报不中断。

        声音从哪来：
        * 默认（``AVATAR_VIDEO_HAS_AUDIO=true``）视频自带音轨，口型与语音天然同步；
        * 若引擎返回的是无声视频（如用示例 mp4 空跑），置 False 则额外下发 ``ai_audio``，
          让学生至少能听到 TTS 语音。
        """
        prefetch: "asyncio.Task[Any] | None" = None
        try:
            epoch = self.session.speech_epoch    # 学生插话会自增，旧播报随即作废
            first = True
            tts_stream = (
                self.tts.synthesize_stream(speech, teacher_id=self._teacher_id())
                if isinstance(speech, str)
                else self._tts_sentence_stream(speech)
            )
            iterator = tts_stream.__aiter__()

            async def _next_sentence() -> Any:
                """取下一句 ``(音频, 文本)``；句子流结束返回 None。"""
                try:
                    return await iterator.__anext__()
                except StopAsyncIteration:
                    return None

            # 预取流水线：把「下一句的 TTS 合成」放进后台任务，与当前句的引擎渲染真正并行。
            # 之前是「渲染完一句 → 才去取下一句 → 取的时候才合成 TTS」，两段完全串行；
            # 现在当前句渲染（GPU 耗时）期间，后台已在合成下一句，句间停顿与首响一并下降。
            prefetch = asyncio.create_task(_next_sentence())
            pending: tuple[bytes, str, "asyncio.Task[dict[str, Any] | None]"] | None = None

            async def _flush_prev() -> bool:
                """等上一句渲染完成并下发；返回 False 表示渲染失败（需回退木偶）。"""
                nonlocal pending
                if pending is None:
                    return True
                prev_audio, prev_sentence, task = pending
                pending = None
                result = await task
                if result is None:
                    return False
                self.session.push_event("ai_avatar", result)
                if not settings.avatar_video_has_audio and prev_audio:
                    self.session.push_event(
                        "ai_audio",
                        {
                            "audio": base64.b64encode(prev_audio).decode("ascii"),
                            "mime": "audio/mpeg",
                            "text": prev_sentence,
                        },
                    )
                return True

            async def _cancel_pending() -> None:
                """作废在途的预取与渲染任务（学生插话/回退时调用），并回收异常避免告警。"""
                nonlocal pending, prefetch
                tasks: list[asyncio.Task[Any]] = []
                if prefetch is not None:
                    prefetch.cancel()
                    tasks.append(prefetch)
                    prefetch = None
                if pending is not None:
                    pending[2].cancel()
                    tasks.append(pending[2])
                    pending = None
                for task in tasks:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 仅回收任务
                        pass

            while True:
                item = await prefetch
                prefetch = None
                if item is None:
                    break                       # 句子流结束：收尾最后一句
                if self.session.speech_epoch != epoch:
                    log.info("avatar speech superseded by new student input; stop rendering")
                    await _cancel_pending()
                    return True
                audio_chunk, sentence = item
                if not audio_chunk and not settings.avatar_allow_silent_audio:
                    # 无音频（如未配置 TTS 的 Mock）：交给调用方走文本回退链路
                    await _cancel_pending()
                    return False
                # 立刻启动下一句 TTS 预取，使其与「当前句渲染」重叠
                prefetch = asyncio.create_task(_next_sentence())
                # 先收上一句（此刻下一句 TTS 已与上一句渲染并行进行）
                if not await _flush_prev():
                    await _cancel_pending()
                    return False
                # 手势只在首句叠加（点头/摇头一次即可），后续句子回到纯讲解，避免反复做同一动作
                pending = (
                    audio_chunk,
                    sentence,
                    asyncio.create_task(self._render_avatar_sentence_padded(
                        audio_chunk, sentence, gesture if first else None)),
                )
                first = False
            if not await _flush_prev():
                return False
        except Exception as err:  # noqa: BLE001 - 渲染失败回退木偶模式
            log.warning("avatar render failed, fallback to puppet: {}", err)
            return False
        finally:
            if prefetch is not None:
                if not prefetch.done():
                    prefetch.cancel()
                try:
                    await prefetch
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 仅回收预取任务
                    pass
        return True
