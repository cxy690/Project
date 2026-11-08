"""微信客服消息处理：回调事件 → 拉取消息 → 回复家长。

微信客服和公众号的回复方式不同，必须分清：

* 公众号 / 企微应用：可以**被动回复** XML，回调里直接返回内容；
* 微信客服：**不支持被动回复**，回调只告诉你「有新消息了」，
  必须自己调 ``sync_msg`` 拉取、再用 ``send_msg`` 主动下发。

而且微信只等回调 5 秒，拉取 + 处理 + 发送是 3 次网络请求，很容易超时，
所以回调里立刻返回 ``success``，真正的处理丢到后台任务（fire-and-forget）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from config.logging import get_logger
from wechat import handlers
from wechat.config import WeChatConfig, load_config
from wechat.kf import (
    FAIL_TYPES,
    MAX_PAGES,
    KfClient,
    get_kf_client,
    load_cursor,
    save_cursor,
)

log = get_logger("wechat.kf_service")

#: 后台任务引用（不持有的话可能被 GC 掉）
_TASKS: set[asyncio.Task] = set()

#: 菜单消息点击后回传的是显示文案（「查学情」），映射回指令关键词
_MENU_ALIAS: dict[str, str] = {
    "xueqing": "学情",
    "kebiao": "课表",
    "bangzhu": "帮助",
}


async def handle_callback(
    token: str,
    open_kfid: str = "",
    config: WeChatConfig | None = None,
) -> int:
    """处理一次 ``kf_msg_or_event`` 回调，返回实际回复的条数。"""
    cfg = config or load_config()
    if not cfg.kf_enabled:
        log.info("微信客服未启用，忽略回调事件")
        return 0

    client = get_kf_client()
    kfid = open_kfid or await client.resolve_open_kfid()
    if not kfid:
        log.warning("微信客服找不到 open_kfid，回调无法处理")
        return 0

    handled = 0
    cursor = load_cursor(kfid, cfg)
    for _ in range(MAX_PAGES):
        data = await client.sync_msg(open_kfid=kfid, cursor=cursor, token=token)
        if not data:
            break
        next_cursor = str(data.get("next_cursor") or "")
        messages = data.get("msg_list") or []
        # 先落盘再处理：宁可极端情况下丢一条，也不能让家长收到重复回复
        if next_cursor:
            save_cursor(kfid, next_cursor, cfg)
            cursor = next_cursor
        for message in messages:
            if isinstance(message, dict):
                handled += await _dispatch(client, message, kfid, cfg)
        if not int(data.get("has_more") or 0):
            break
    return handled


async def poll_once(config: WeChatConfig | None = None) -> int:
    """不依赖回调的手动补拉（实例重启丢事件时用）。

    官方对「不带 token 拉取」有严格频率限制，只适合人工或低频补拉，
    不要挂到高频定时任务上。
    """
    return await handle_callback("", "", config)


async def _dispatch(
    client: KfClient,
    message: dict[str, Any],
    open_kfid: str,
    cfg: WeChatConfig,
) -> int:
    """分派单条消息：文本走指令解析，事件走各自的处理。"""
    msgtype = str(message.get("msgtype") or "").lower()
    origin = int(message.get("origin") or 0)
    external = str(message.get("external_userid") or "")

    if msgtype == "event":
        event = message.get("event") or {}
        return await _handle_event(client, event if isinstance(event, dict) else {}, open_kfid, cfg)

    # origin：3-微信客户发送 4-系统事件 5-接待人员（企业成员）发送
    if origin != 3 or not external:
        return 0

    if msgtype != "text":
        ok = await client.send_text(external, handlers.MEDIA_REPLY, open_kfid=open_kfid)
        return 1 if ok else 0

    text_obj = message.get("text") or {}
    content = str((text_obj or {}).get("content") or "")
    menu_id = str((text_obj or {}).get("menu_id") or "")
    if menu_id in _MENU_ALIAS:
        content = _MENU_ALIAS[menu_id]

    reply = handlers.handle_text(external, content, cfg)
    if not reply:
        return 0
    ok = await client.send_text(external, reply, open_kfid=open_kfid)
    return 1 if ok else 0


async def _handle_event(
    client: KfClient,
    event: dict[str, Any],
    open_kfid: str,
    cfg: WeChatConfig,
) -> int:
    """处理微信客服事件。"""
    event_type = str(event.get("event_type") or "").lower()
    external = str(event.get("external_userid") or "")

    if event_type == "enter_session":
        code = str(event.get("welcome_code") or "")
        if not code:
            # 过去 48 小时已经收过欢迎语 / 发过消息，官方不再下发 code
            return 0
        text = handlers.welcome_text(external, cfg)
        return 1 if await client.send_welcome(code, text) else 0

    if event_type == "msg_send_fail":
        fail_type = int(event.get("fail_type") or 0)
        reason = FAIL_TYPES.get(fail_type, "未知原因")
        log.warning(
            "微信客服消息未送达（{}）：fail_type={} {}",
            external,
            fail_type,
            reason,
        )
        await _alert_staff(external, reason, cfg)
        return 0

    return 0


async def _alert_staff(external_userid: str, reason: str, cfg: WeChatConfig) -> None:
    """家长没收到时提醒老师手动跟进（发到企微群）。

    这是免费方案的关键兜底：48 小时窗口过期是微信的硬规则，
    代码绕不过去，只能让人接住。
    """
    if not cfg.webhook_enabled:
        return
    from wechat.client import get_client  # noqa: PLC0415

    text = (
        f"【微信客服未送达】家长 {external_userid}\n"
        f"原因：{reason}\n"
        "请老师手动跟进这节课的学情。"
    )
    if not await get_client().send_group_text(text):
        log.warning("微信客服失败提醒也没发出去（群机器人未配置或网络异常）")


def schedule_callback(token: str, open_kfid: str = "") -> None:
    """回调里 fire-and-forget：微信只等 5 秒，处理必须后台跑。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环（脚本同步调用）就放弃，别阻塞
        log.warning("微信客服回调没有可用事件循环，已跳过")
        return
    task = loop.create_task(handle_callback(token, open_kfid))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
