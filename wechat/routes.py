"""微信回调路由（公众号 / 企业微信应用）。

两个入口：

* ``GET  /wechat/callback``  微信服务器配置回调地址时的 URL 验证（回 echostr）；
* ``POST /wechat/callback``  接收消息（明文或安全模式），交给 :mod:`wechat.handlers`，
  同步返回被动回复 XML。

**没配 WECHAT_TOKEN 时整个路由返回 404** —— 不暴露一个没人验签的公开入口。
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse

from config.logging import get_logger
from wechat import handlers
from wechat.config import WeChatConfig, load_config
from wechat.crypto import (
    WeChatCryptoError,
    decrypt_message,
    encrypt_message,
    sign_message,
    verify_msg_signature,
    verify_signature,
)
from wechat.kf_service import schedule_callback as schedule_kf_callback

log = get_logger("wechat.routes")

router = APIRouter(tags=["wechat"])

#: 微信回调超时是 5 秒，我们内部处理必须是纯内存/文件操作（当前实现满足）
_REPLY_TEMPLATE = (
    "<xml>"
    "<ToUserName><![CDATA[{to}]]></ToUserName>"
    "<FromUserName><![CDATA[{from_}]]></FromUserName>"
    "<CreateTime>{ts}</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[{content}]]></Content>"
    "</xml>"
)


def _cfg() -> WeChatConfig:
    return load_config()


def _enabled(config: WeChatConfig) -> bool:
    return bool(config.token)


def _parse_fields(xml_text: str) -> dict[str, str]:
    """把微信 XML 拍平成 ``{标签: 文本}``（够用即可，不做完整 schema）。"""
    fields: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        log.warning("微信回调 XML 解析失败：{}", err)
        return fields
    for child in root:
        fields[str(child.tag)] = (child.text or "").strip()
    return fields


def _reply_xml(to_user: str, from_user: str, content: str) -> str:
    # CDATA 里不能出现 "]]>"，先拆开
    safe = str(content or "").replace("]]>", "]]]]><![CDATA[>")
    return _REPLY_TEMPLATE.format(
        to=to_user, from_=from_user, ts=int(time.time()), content=safe
    )


def _encrypted_reply(xml_text: str, config: WeChatConfig, receiver: str) -> str:
    """安全模式回复：加密 + 重新签名，包成微信要求的 XML。"""
    encrypt = encrypt_message(config.encoding_aes_key, xml_text, receiver)
    timestamp = str(int(time.time()))
    nonce = str(int(time.time() * 1000) % 100000)
    signature = sign_message(config.token, timestamp, nonce, encrypt)
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )


@router.get("/wechat/callback")
async def wechat_verify(
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
    msg_signature: str = Query(default=""),
) -> Response:
    """微信服务器 URL 验证：验签通过回 echostr。

    两种形态，必须都支持，否则后台「保存」一直失败：

    * 公众号 / 企业微信（明文模式）：带 ``signature``，echostr 直接回显；
    * 企业微信（加密模式，后台默认）：带 ``msg_signature``，且 ``echostr``
      本身是密文 —— 要先验签、再解密，回**明文**。
    """
    config = _cfg()
    if not _enabled(config):
        return PlainTextResponse("wechat disabled", status_code=404)

    if msg_signature:
        if not config.encoding_aes_key:
            return PlainTextResponse("aes key not configured", status_code=403)
        if not verify_msg_signature(config.token, timestamp, nonce, echostr, msg_signature):
            log.warning("企业微信 URL 验证签名不通过")
            return PlainTextResponse("invalid signature", status_code=403)
        try:
            plain = decrypt_message(
                config.encoding_aes_key, echostr, config.corp_id or config.app_id
            )
        except WeChatCryptoError as err:
            log.warning("企业微信 URL 验证解密失败：{}", err)
            return PlainTextResponse("decrypt failed", status_code=403)
        return PlainTextResponse(plain)

    if not verify_signature(config.token, timestamp, nonce, signature):
        log.warning("微信 URL 验证签名不通过")
        return PlainTextResponse("invalid signature", status_code=403)
    return PlainTextResponse(echostr)


@router.post("/wechat/callback")
async def wechat_callback(
    request: Request,
    signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    msg_signature: str = Query(default=""),
    encrypt_type: str = Query(default=""),
) -> Response:
    """接收微信消息并被动回复。"""
    config = _cfg()
    if not _enabled(config):
        return PlainTextResponse("wechat disabled", status_code=404)

    raw = await request.body()
    xml_text = raw.decode("utf-8", "replace")
    encrypted = encrypt_type.lower() == "aes" or "Encrypt>" in xml_text

    if encrypted:
        if not config.encoding_aes_key:
            return PlainTextResponse("aes key not configured", status_code=403)
        holder = _parse_fields(xml_text)
        encrypt = holder.get("Encrypt", "")
        if not verify_msg_signature(
            config.token, timestamp, nonce, encrypt, msg_signature
        ):
            log.warning("微信消息签名不通过")
            return PlainTextResponse("invalid signature", status_code=403)
        try:
            xml_text = decrypt_message(
                config.encoding_aes_key, encrypt, config.corp_id or config.app_id
            )
        except WeChatCryptoError as err:
            log.warning("微信消息解密失败：{}", err)
            return PlainTextResponse("decrypt failed", status_code=403)
    elif not verify_signature(config.token, timestamp, nonce, signature):
        log.warning("微信消息签名不通过（明文）")
        return PlainTextResponse("invalid signature", status_code=403)

    fields = _parse_fields(xml_text)
    to_user = fields.get("ToUserName", "")          # 公众号 / 企业微信
    from_user = fields.get("FromUserName", "")      # 家长 open_id
    msg_type = fields.get("MsgType", "").lower()
    content = fields.get("Content", "")
    event = fields.get("Event", "")

    if msg_type == "event":
        if event.lower() == "kf_msg_or_event":
            # 微信客服不支持被动回复：回调里只给「Token + OpenKfId」，
            # 必须自己 sync_msg 拉消息、再 send_msg 主动发。微信只等 5 秒，
            # 所以立刻 200，真正的处理丢后台任务。
            schedule_kf_callback(fields.get("Token", ""), fields.get("OpenKfId", ""))
            return PlainTextResponse("success")
        reply = handlers.handle_event(event.lower(), from_user)
    elif msg_type == "text":
        reply = handlers.handle_text(from_user, content, config)
    else:
        reply = ""

    if not reply:
        return PlainTextResponse("success")

    reply_xml = _reply_xml(from_user, to_user, reply)
    if encrypted:
        return Response(
            content=_encrypted_reply(reply_xml, config, config.corp_id or config.app_id),
            media_type="application/xml",
        )
    return Response(content=reply_xml, media_type="application/xml")
