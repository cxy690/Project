"""WebSocket 会话端点：把客户端与 :class:`EducationHarness` 连接起来。

入站消息 -> SessionLoop.submit；出站由 SessionState.emit 回调推入发送队列，
由独立 sender 任务异步下发，避免回调是同步的而 WS 发送是异步的冲突。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from config.logging import get_logger
from config.settings import settings
from config.stages import normalize_stage, stage_from_grade, stage_label
from config.subjects import resolve_subject
from core.auth.deps import user_from_token
from core.harness import EducationHarness
from core.learning import events as learning_events
from core.learning_stats import get_collector
from core.state.store import session_store

log = get_logger("server.websocket")

# 进程级装配根（与 server 同生命周期）
harness = EducationHarness()


def _observe_message(state: Any, message: dict[str, Any]) -> None:
    """把学生侧消息交给学情采集器（旁路，异常不影响主链路）。"""
    try:
        kind = str(message.get("type") or "")
        collector = get_collector()
        if kind == "text":
            collector.student_message(state, str(message.get("text") or ""), "text")
        elif kind == "voice":
            collector.student_message(state, "", "voice")
        elif kind == "interrupt":
            collector.interrupt(state)
        elif kind == "course_start":
            collector.start(state)
    except Exception as err:  # noqa: BLE001 - 学情采集是旁路
        log.warning("learning observe skipped: {}", err)


def _query_token(websocket: WebSocket) -> str:
    """从 query string 取令牌（浏览器 WS 无法自定义 Header，故走 URL）。"""
    params = websocket.query_params
    return (params.get("token") or params.get("access_token") or "").strip()


async def _reject(websocket: WebSocket, code: int, message: str) -> None:
    """已 accept 后拒绝连接：先下发 error 事件，再按业务码关闭。"""
    try:
        await websocket.send_json(
            {"type": "error", "payload": {"code": code, "message": message}}
        )
        await websocket.close(code=code)
    except RuntimeError:
        pass


async def websocket_endpoint(websocket: WebSocket) -> None:
    """单条 WebSocket 连接 = 一个学生答疑会话。

    身份：先按 ``?token=<JWT>``（或 init 消息里的 ``token``）校验，通过后
    ``student_id`` 一律取自令牌主体（不再信任前端自报）；未登录时按灰度策略：
    ``AUTH_REQUIRED=true`` 直接拒绝，``false`` 则允许匿名并以客户端自报 id 兜底。
    """
    await websocket.accept()
    student_id = "anonymous"
    out_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(payload: dict[str, Any]) -> None:
        # 同步回调：仅入队，由 sender 任务真正下发
        out_queue.put_nowait(payload)

    state = None
    supervisor = None
    loop = None
    sender: asyncio.Task | None = None
    runner: asyncio.Task | None = None
    resumed = False
    end_reason = "disconnect"

    try:
        # 等待 init 消息（可携带 student_id / session_id / 学科 / token）
        first = await websocket.receive_json()
        if not isinstance(first, dict):
            first = {}

        # ---- 身份校验（认证）----
        token = _query_token(websocket) or str(first.get("token") or "").strip()
        user = await asyncio.to_thread(user_from_token, token) if token else None
        if settings.auth_enable:
            if token and user is None:
                await _reject(websocket, 4401, "身份凭证无效或已过期")
                return
            if not token and settings.auth_required:
                await _reject(websocket, 4401, "需要登录后才能连接")
                return

        if first.get("type") == "init":
            # 已认证时以令牌主体为准，忽略客户端自报的 student_id
            student_id = str((user or {}).get("id") or first.get("student_id") or student_id)
            # ---- 限流（防滥用）----
            if not harness.anti_rev.allow(student_id):
                await _reject(websocket, 4429, "请求过于频繁，请稍后再试")
                return
            requested_session_id = str(first.get("session_id") or "").strip() or None
            existing = session_store.get(requested_session_id) if requested_session_id else None
            resumed = bool(existing is not None and existing.student_id == student_id)
            state, supervisor, loop = harness.create_session(
                student_id,
                session_id=requested_session_id,
                emit=emit,
            )
            if user is not None:
                state.scratch["auth"] = {
                    "id": user["id"],
                    "role": user["role"],
                    "username": user["username"],
                }
                # 学段注入：出题按学生年级从真题库（ai_teacher.exercise_item）选题，
                # 提示词（teaching_layers / 教师人格）也从 scratch["grade"] 读学段。
                # 用户未填年级时不写，下游按「不限年级」处理。
                _grade = str(user.get("grade") or "").strip()
                if _grade:
                    state.scratch["grade"] = _grade
                # 学段注入：注册时选择的学段决定加载哪套学科技能
                # （.agents/skills/<学科>/primary|junior/SKILL.md）。
                # 老账号没有 stage 字段时按年级推导，仍无法判定就加载通用版。
                _stage = normalize_stage(user.get("stage")) or stage_from_grade(_grade)
                if _stage:
                    state.scratch["stage"] = _stage
                    state.scratch["stage_label"] = stage_label(_stage)
            # 未登录（灰度/演示）或老客户端：允许 init 显式带学段，仅作兜底；
            # 已登录用户始终以数据库里的 stage 为准（上面优先写入）。
            if not state.scratch.get("stage"):
                _fallback_stage = normalize_stage(first.get("stage"))
                if not _fallback_stage:
                    _fallback_stage = stage_from_grade(
                        str(first.get("grade") or "").strip()
                    )
                if _fallback_stage:
                    state.scratch["stage"] = _fallback_stage
                    state.scratch["stage_label"] = stage_label(_fallback_stage)
            raw_subject = str(first.get("subject") or "").strip()
            if raw_subject:
                state.subject = resolve_subject(raw_subject)
            # 同科多老师：学生在选择界面挑中的老师档案 key（为空=学科默认老师）
            raw_teacher = str(first.get("teacher") or "").strip()
            if raw_teacher:
                state.teacher = raw_teacher
                state.scratch["teacher"] = raw_teacher
        else:
            if not harness.anti_rev.allow(student_id):
                await _reject(websocket, 4429, "请求过于频繁，请稍后再试")
                return
            state, supervisor, loop = harness.create_session(student_id, emit=emit)

        # 学情事件从这里开始：session_id 是后续所有课堂事件的归属键。
        # 采集失败静默，不影响 ready / 教学链路。
        try:
            learning_events.record_session_start(state, resumed=resumed)
        except Exception as err:  # noqa: BLE001
            log.warning("learning session start failed: {}", err)

        await websocket.send_json({
            "type": "ready",
            "payload": {"session_id": state.session_id, "resumed": resumed},
        })

        async def sender_loop() -> None:
            while True:
                payload = await out_queue.get()
                await websocket.send_json(payload)

        sender = asyncio.create_task(sender_loop())
        runner = asyncio.create_task(loop.run())

        # 主接收循环
        while True:
            message = await websocket.receive_json()
            mtype = message.get("type")
            if mtype == "close":
                end_reason = "client_close"
                break
            # 学情旁路：心跳带页面可见性，用来计算「有效在场」而不是挂机时长；
            # 只有 ping 与 course_start 需要在这里采集，其余交给 SessionState.push_event。
            try:
                if mtype == "ping":
                    state.touch()
                    learning_events.record_heartbeat(
                        state, visible=bool(message.get("visible", True))
                    )
                elif mtype == "course_start":
                    learning_events.record_course_start(
                        state,
                        subject=str(message.get("subject") or ""),
                        teacher=str(message.get("teacher") or ""),
                    )
            except Exception as err:  # noqa: BLE001 - 采集失败不影响消息分发
                log.warning("learning event record failed: {}", err)
            _observe_message(state, message)
            loop.submit(message)

    except WebSocketDisconnect:
        log.info("client disconnected: {}", student_id)
    except Exception as err:  # noqa: BLE001 - 保证连接异常不拖垮进程
        end_reason = "error"
        log.exception("websocket error: {}", err)
    finally:
        if loop is not None:
            loop.stop()
        for task in (runner, sender):
            if task is not None:
                task.cancel()
        # 先等取消真正落地，再做会话收尾：否则 runner 里正在跑的回合（答疑 / 课件连讲 /
        # 数字人渲染）会与 on_session_end 并发改同一份 SessionState，状态可能互相覆盖。
        await asyncio.gather(
            *(task for task in (runner, sender) if task is not None),
            return_exceptions=True,
        )
        # Agent 生命周期收尾（Supervisor 是 BaseAgent 子类）：停掉本会话后台任务
        if supervisor is not None:
            try:
                await supervisor.on_session_end(supervisor.session)
            except Exception as err:  # noqa: BLE001 - 收尾失败不该影响断连
                log.warning("on_session_end failed: {}", err)
        if state is not None:
            harness.checkpoint(state)
            # 先落 session_end 再聚合报告：事件流报告要包含「离开」这一事实。
            try:
                await asyncio.to_thread(
                    learning_events.finish_session, state, end_reason
                )
            except Exception as err:  # noqa: BLE001 - 学情收尾失败不影响断连
                log.warning("learning session finish failed: {}", err)
            # 学情结算：落盘指标 + 更新学生索引（异常已在采集器内部吞掉）
            metrics = get_collector().finish(state, reason="disconnect")
            if metrics is not None:
                # 课后学情通知：fire-and-forget，未配置微信时内部直接跳过
                try:
                    from wechat.notifier import schedule_lesson_report  # noqa: PLC0415

                    schedule_lesson_report(metrics)
                except Exception as err:  # noqa: BLE001 - 通知失败不影响下课
                    log.warning("learn report notify skipped: {}", err)
        try:
            await websocket.close()
        except RuntimeError:
            pass
