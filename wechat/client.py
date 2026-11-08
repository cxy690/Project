"""微信消息发送客户端（公众号客服消息 / 企业微信应用消息 / 群机器人）。

设计原则：**发不出去也不能影响主流程**。所有方法返回 ``bool``，
失败只记日志；学情推送是增值功能，不能因为微信接口抖动把课堂搞崩。

access_token 缓存 7000 秒（微信官方有效期 7200 秒），提前刷新避免临界过期。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from config.logging import get_logger
from wechat.config import WeChatConfig, load_config

log = get_logger("wechat.client")

WX_API = "https://api.weixin.qq.com"
WX_CORP_API = "https://qyapi.weixin.qq.com"
TOKEN_TTL = 7000          # 秒，官方 7200，留 200 秒余量
HTTP_TIMEOUT = 10.0       # 秒

#: 正文长度上限，单位是**字节**不是字符（官方口径）。
#: 中文一个字 3 字节，按字符截断会算出 3 倍长度，直接被接口拒收。
TEXT_LIMIT_BYTES = 2048
MARKDOWN_LIMIT_BYTES = 4096

_TRUNCATED_MARK = "\n…（已截断）"


def truncate_bytes(text: str, limit: int) -> str:
    """按 UTF-8 **字节数**截断，不切半个汉字。

    微信 / 企业微信所有正文限制都是字节口径，用 ``text[:n]`` 这种字符切片
    在纯中文场景下会超出 2—3 倍，表现为 ``errcode 40058`` / 消息被拒。
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    mark = _TRUNCATED_MARK.encode("utf-8")
    keep = max(0, limit - len(mark))
    # errors="ignore" 只会丢掉被切断的那个残缺字符的尾字节，不会产生乱码
    return raw[:keep].decode("utf-8", errors="ignore") + _TRUNCATED_MARK


def split_bytes(text: str, limit: int, max_parts: int = 3) -> list[str]:
    """把长文按段落切成多片，每片不超过 ``limit`` 字节。

    优先在换行处切，避免把一句话切开；超出 ``max_parts`` 的部分丢弃并加提示。
    """
    if len(text.encode("utf-8")) <= limit:
        return [text]
    parts: list[str] = []
    buffer = ""
    for line in text.split("\n"):
        candidate = f"{buffer}\n{line}" if buffer else line
        if len(candidate.encode("utf-8")) <= limit:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
        buffer = line
    if buffer:
        parts.append(buffer)
    if len(parts) > max_parts:
        parts = parts[:max_parts]
        parts[-1] = truncate_bytes(parts[-1], limit)
        parts[-1] += f"\n…（共 {len(parts)} 片，其余内容请到后台查看）"
    return [truncate_bytes(p, limit) for p in parts]


@dataclass
class _TokenCache:
    value: str = ""
    expires_at: float = 0.0

    def alive(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at

    def store(self, value: str) -> None:
        self.value = value
        self.expires_at = time.time() + TOKEN_TTL


@dataclass
class WeChatClient:
    """一个进程一个实例即可（token 与连接复用）。"""

    config: WeChatConfig = field(default_factory=load_config)
    _token: _TokenCache = field(default_factory=_TokenCache)

    # ------------------------------------------------------------ token
    async def access_token(self) -> str:
        """取 access_token（带缓存）；未配置或失败返回空串。"""
        if self._token.alive():
            return self._token.value
        if not (self.config.app_secret and (self.config.corp_id or self.config.app_id)):
            return ""
        try:
            import httpx  # noqa: PLC0415 - 延迟导入，纯本地使用时不必装
        except Exception:  # noqa: BLE001
            log.warning("httpx 不可用，微信发送已跳过")
            return ""
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                if self.config.is_corp:
                    resp = await client.get(
                        f"{WX_CORP_API}/cgi-bin/gettoken",
                        params={"corpid": self.config.corp_id, "corpsecret": self.config.app_secret},
                    )
                else:
                    resp = await client.get(
                        f"{WX_API}/cgi-bin/token",
                        params={
                            "grant_type": "client_credential",
                            "appid": self.config.app_id,
                            "secret": self.config.app_secret,
                        },
                    )
                data = resp.json()
        except Exception as err:  # noqa: BLE001 - 网络异常降级
            log.warning("微信 access_token 获取失败：{}", err)
            return ""
        token = str(data.get("access_token") or "")
        if not token:
            log.warning("微信 access_token 返回异常：{}", data)
            return ""
        self._token.store(token)
        return token

    # ------------------------------------------------------------ 发消息
    async def send_text(self, open_id: str, text: str) -> bool:
        """给单个用户发文本（公众号客服消息 / 企微应用消息）。"""
        if not open_id or not text:
            return False
        token = await self.access_token()
        if not token:
            return False
        try:
            import httpx  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return False
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                if self.config.is_corp:
                    url = f"{WX_CORP_API}/cgi-bin/message/send?access_token={token}"
                    body = {
                        "touser": open_id,
                        "msgtype": "text",
                        "agentid": int(self.config.agent_id or 0),
                        "text": {"content": truncate_bytes(text, TEXT_LIMIT_BYTES)},
                    }
                else:
                    url = f"{WX_API}/cgi-bin/message/custom/send?access_token={token}"
                    body = {
                        "touser": open_id,
                        "msgtype": "text",
                        "text": {"content": truncate_bytes(text, TEXT_LIMIT_BYTES)},
                    }
                resp = await client.post(url, json=body)
                data = resp.json()
        except Exception as err:  # noqa: BLE001
            log.warning("微信消息发送失败（{}）：{}", open_id, err)
            return False
        if int(data.get("errcode") or 0) != 0:
            log.warning("微信消息发送被拒（{}）：{}", open_id, data)
            return False
        return True

    async def send_group_text(self, text: str, webhook: str = "") -> bool:
        """往企业微信群机器人发文本（群公告 / 学情通知）。

        正文超过 2048 字节时按段落拆成最多 3 片依次发送 —— 班级周报、
        多学生明细很容易超过上限，直接截断会让家长看到半句话。
        """
        url = webhook or self.config.webhook_url
        if not url or not text:
            return False
        try:
            import httpx  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return False
        parts = split_bytes(text, TEXT_LIMIT_BYTES)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                for index, part in enumerate(parts):
                    resp = await client.post(
                        url, json={"msgtype": "text", "text": {"content": part}}
                    )
                    data = resp.json()
                    if int(data.get("errcode") or 0) != 0:
                        log.warning("微信群消息被拒（第 {}/{} 片）：{}", index + 1, len(parts), data)
                        return False
        except Exception as err:  # noqa: BLE001
            log.warning("微信群消息发送失败：{}", err)
            return False
        return True

    async def send_markdown(self, text: str, webhook: str = "") -> bool:
        """群机器人 markdown（企业微信群机器人支持，公众号不支持）。"""
        url = webhook or self.config.webhook_url
        if not url or not text:
            return False
        try:
            import httpx  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return False
        text = truncate_bytes(text, MARKDOWN_LIMIT_BYTES)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={"msgtype": "markdown", "markdown": {"content": text}},
                )
                data = resp.json()
        except Exception as err:  # noqa: BLE001
            log.warning("微信群 markdown 发送失败：{}", err)
            return False
        return int(data.get("errcode") or 0) == 0


_CLIENT = WeChatClient()


def get_client() -> WeChatClient:
    return _CLIENT


def reset_client(config: WeChatConfig | None = None) -> WeChatClient:
    """重建客户端（测试 / 热改配置用）。"""
    global _CLIENT
    _CLIENT = WeChatClient(config=config or load_config())
    return _CLIENT
