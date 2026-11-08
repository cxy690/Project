"""微信/企微消息发送适配器。

只实现官方或合规通道：
* ``official_account``：按用户授权微信身份发送真实学情模板消息；
* ``mock``：写本地 outbox，用于本地演示与测试；
* ``wecom_webhook``：企业微信群机器人 webhook；
* ``serverchan``：Server酱 SendKey，可推送到个人微信。

不接入个人微信自动化（hook / 协议号），避免账号被封与合规风险。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from config.logging import get_logger
from config.settings import settings

log = get_logger("core.wechat.providers")
ALLOWED_CHANNELS = ("official_account", "mock", "wecom_webhook", "serverchan")


@dataclass
class DeliveryResult:
    ok: bool
    message_id: str = ""
    error: str = ""


def _post_json(url: str, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 管理台配置的官方 webhook
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"raw": raw}


def _post_form(url: str, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    data = urlencode({k: str(v) for k, v in payload.items()}).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 管理台配置的官方接口
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"raw": raw}


def channel_status(channel: str, target: str = "") -> dict[str, Any]:
    """仅校验配置，不联网，不返回凭据；配置就绪不代表微信已收到消息。"""
    ch = (channel or "").strip().lower()
    dest = (target or "").strip()
    source = "target" if dest else "global"
    error = ""
    if ch == "official_account":
        from core.wechat.official import configuration

        config = configuration()
        return {"channel": ch, "configured": config["send_ready"], "simulated": False,
                "source": "authorization", "error": config["send_error"],
                "label": "服务号已配置，按用户授权身份发送" if config["send_ready"] else "服务号配置未完成"}
    if ch == "mock":
        return {"channel": ch, "configured": True, "simulated": True,
                "source": "local", "error": "", "label": "仅本地模拟，不会发送微信"}
    if ch == "wecom_webhook":
        dest = dest or (settings.wechat_wecom_webhook or "").strip()
        try:
            parsed = urlparse(dest)
        except ValueError:
            parsed = urlparse("")
        if not (
            parsed.scheme == "https" and parsed.netloc == "qyapi.weixin.qq.com"
            and parsed.path == "/cgi-bin/webhook/send"
            and parse_qs(parsed.query).get("key") and not parsed.fragment
        ):
            error = "请配置有效的企业微信群机器人 HTTPS webhook"
    elif ch == "serverchan":
        dest = dest or (settings.wechat_serverchan_key or "").strip()
        if dest.startswith("https://sctapi.ftqq.com/") and dest.endswith(".send"):
            dest = dest[len("https://sctapi.ftqq.com/"):-len(".send")]
        if not dest or not all(c.isascii() and (c.isalnum() or c in "-_") for c in dest):
            error = "请配置有效的 Server酱 SendKey（或 sctapi.ftqq.com 发送地址）"
    else:
        error = "不支持的渠道"
    return {"channel": ch, "configured": not bool(error), "simulated": False,
            "source": source, "error": error,
            "label": "已配置，需测试接收" if not error else "未配置或配置无效"}


def _mock_send(title: str, text: str) -> DeliveryResult:
    root = Path(settings.wechat_dir or "data/wechat") / "outbox"
    day = time.strftime("%Y-%m-%d")
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
    (folder / name).write_text(
        json.dumps(
            {"title": title, "text": text, "created_at": time.time()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return DeliveryResult(ok=True, message_id="mock:" + name)


def send_message(
    channel: str, target: str, *, title: str, text: str,
    guardian_id: str = "", report: dict[str, Any] | None = None,
) -> DeliveryResult:
    """按渠道发送；``target`` 为空时回退到全局配置。"""
    ch = (channel or settings.wechat_provider or "mock").strip().lower()
    dest = (target or "").strip()
    state = channel_status(ch, dest)
    if ch == "official_account" and report and report.get("kind") == "course_update":
        from core.wechat.official import configuration
        config = configuration()
        state = {"configured": config["course_ready"], "error": config["course_error"]}
    if not state["configured"]:
        return DeliveryResult(ok=False, error=state["error"])
    if ch == "official_account":
        from sqlalchemy import select
        from core.wechat import official
        from db.base import get_session
        from db.models import GuardianLink, User, WeChatIdentity, account_role

        if dest or not guardian_id or report is None:
            return DeliveryResult(ok=False, error="服务号仅按用户授权身份发送真实学情，不支持手输目标或通用测试消息")
        from sqlalchemy import update

        with get_session() as db:
            db.execute(update(WeChatIdentity).where(WeChatIdentity.guardian_id == guardian_id)
                       .values(enabled=WeChatIdentity.enabled))
            identity = db.get(WeChatIdentity, guardian_id)
            parent = db.get(User, guardian_id)
            student_id = str(report.get("student", {}).get("id") or "")
            db.execute(update(GuardianLink).where(
                GuardianLink.guardian_id == guardian_id, GuardianLink.student_id == student_id,
            ).values(relation=GuardianLink.relation))
            linked = db.scalar(select(GuardianLink.id).where(
                GuardianLink.guardian_id == guardian_id, GuardianLink.student_id == student_id,
            ))
            if (parent is None or account_role(parent.role) not in ("user", "admin") or not parent.is_active
                    or (guardian_id != student_id and linked is None)):
                return DeliveryResult(ok=False, error="接收账号或数据授权无效")
            if (identity is None or not identity.enabled
                    or identity.app_id != settings.wechat_official_app_id.strip()):
                return DeliveryResult(ok=False, error="用户微信未绑定、通知已关闭或服务号已变更")
            try:
                sender = official.send_course_update if report.get("kind") == "course_update" else official.send_report
                result = DeliveryResult(ok=True, message_id=sender(identity.openid, report))
            except official.DeliveryUncertain:
                if report.get("kind") == "course_update":
                    raise
                result = DeliveryResult(ok=False, error="微信发送结果不明，请先核对接收端")
            except official.WeChatError as err:
                result = DeliveryResult(ok=False, error=str(err))
            db.commit()
            return result
    if ch == "mock":
        return _mock_send(title, text)
    if ch == "wecom_webhook":
        url = dest or (settings.wechat_wecom_webhook or "").strip()
        try:
            data = _post_json(url, {"msgtype": "markdown", "markdown": {"content": text}})
        except Exception as err:  # noqa: BLE001
            log.warning("企微 webhook 发送失败：{}", type(err).__name__)
            return DeliveryResult(ok=False, error="企业微信网络请求失败，请检查网络后重试")
        if isinstance(data, dict) and str(data.get("errcode")) == "0":
            return DeliveryResult(ok=True, message_id=str(data.get("msgid") or ""))
        return DeliveryResult(ok=False, error="企业微信未确认成功，请检查机器人密钥、限额和服务响应")
    if ch == "serverchan":
        key = dest or (settings.wechat_serverchan_key or "").strip()
        url = key if key.startswith("https://sctapi.ftqq.com/") else f"https://sctapi.ftqq.com/{key}.send"
        try:
            data = _post_form(url, {"title": title[:32], "desp": text})
        except Exception as err:  # noqa: BLE001
            log.warning("Server酱发送失败：{}", type(err).__name__)
            return DeliveryResult(ok=False, error="Server酱网络请求失败，请检查网络后重试")
        if isinstance(data, dict) and str(data.get("code")) == "0":
            info = data.get("data")
            message_id = info.get("pushid", "") if isinstance(info, dict) else ""
            return DeliveryResult(ok=True, message_id=str(message_id))
        return DeliveryResult(ok=False, error="Server酱未确认成功，请检查 SendKey、限额和接收通道")
    return DeliveryResult(ok=False, error="不支持的渠道")


def provider_available(channel: str) -> bool:
    return bool(channel_status(channel)["configured"])


__all__ = ["ALLOWED_CHANNELS", "DeliveryResult", "channel_status", "provider_available", "send_message"]
