"""微信客服（WeCom KF）客户端：拉取消息、发送消息、欢迎语。

与 :mod:`wechat.client` 的分工：

| 通道 | 收件人 | 能否主动下发 |
|---|---|---|
| ``client.py`` | 企微通讯录成员 / 公众号粉丝 | 能（企微应用、公众号客服消息） |
| ``kf.py`` | **微信外部客户（家长）** | 仅 48 小时窗口内，最多 5 条 |

家长扫码即可进入客服会话，不需要关注公众号、不需要加好友，是教育机构
最省事的触达方式。但官方有三条硬约束，决定了本模块的实现方式：

1. **48 小时窗口**：家长主动发消息后的 48 小时内、最多 5 条，企业才能下发。
   窗口外调 ``send_msg`` 会返回成功，但随后通过 ``msg_send_fail`` 事件
   告知 ``fail_type=4``（会话已过期）—— 所以「课后自动推」只能尽力而为；
2. **发送失败是异步的**：接口 ``errcode=0`` 不代表送达，必须处理回调事件，
   失败时降级提醒老师手动跟进；
3. **拉消息必须增量**：``sync_msg`` 依赖 ``cursor``，游标不落盘会从 3 天内
   最早的消息重拉，家长就会收到重复回复 —— 所以游标持久化到
   ``data/wechat/kf_cursor.json``。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.logging import get_logger
from wechat.config import WeChatConfig, load_config

log = get_logger("wechat.kf")

WX_CORP_API = "https://qyapi.weixin.qq.com"
TOKEN_TTL = 7000          # 秒，官方 7200，留 200 秒余量
HTTP_TIMEOUT = 10.0       # 秒
MAX_TEXT_BYTES = 2048     # send_msg 文本上限（字节，不是字符）
MAX_PAGES = 5             # 单次回调最多翻页次数，防止 has_more 无限循环

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CURSOR_FILE = PROJECT_ROOT / "data" / "wechat" / "kf_cursor.json"

#: ``msg_send_fail`` 事件的失败类型（官方文档「事件消息」）
FAIL_TYPES: dict[int, str] = {
    0: "未知原因",
    1: "客服账号已删除",
    2: "应用已关闭",
    4: "会话已过期（超过 48 小时）",
    5: "会话已关闭",
    6: "超过 5 条限制",
    8: "主体未验证",
    10: "用户拒收",
    11: "企业未有成员登录企业微信 App",
    12: "客服组件禁发的消息类型",
    13: "安全限制",
}


# ---------------------------------------------------------------- 游标持久化
def _cursor_path(config: WeChatConfig | None = None) -> Path:
    cfg = config or load_config()
    raw = (cfg.kf_cursor_file or "").strip()
    if not raw:
        return DEFAULT_CURSOR_FILE
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _read_cursors(config: WeChatConfig | None = None) -> dict[str, str]:
    try:
        data = json.loads(_cursor_path(config).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 文件不存在/损坏都按空处理
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def load_cursor(open_kfid: str, config: WeChatConfig | None = None) -> str:
    """取某个客服账号的上次拉取游标（没有就从 3 天内最早的消息开始）。"""
    if not open_kfid:
        return ""
    return _read_cursors(config).get(open_kfid, "")


def save_cursor(open_kfid: str, cursor: str, config: WeChatConfig | None = None) -> None:
    """保存游标（原子写，避免进程被杀时留下半截文件）。"""
    if not (open_kfid and cursor):
        return
    path = _cursor_path(config)
    data = _read_cursors(config)
    data[open_kfid] = cursor
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as err:  # noqa: BLE001 - 游标写失败不该让消息处理崩
        log.warning("微信客服游标保存失败：{}", err)


# ---------------------------------------------------------------- 文本工具
def truncate_utf8(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    """按 **字节** 截断，避免把中文切成半个字（官方限制是 2048 字节）。"""
    raw = str(text or "")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:  # 极少数代理字符，退回按字符截断
        return raw[: limit // 3]
    if len(encoded) <= limit:
        return raw
    return encoded[:limit].decode("utf-8", "ignore")


# ---------------------------------------------------------------- 客户端
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
class KfClient:
    """一个进程一个实例即可（token 复用）。"""

    config: WeChatConfig = field(default_factory=load_config)
    _token: _TokenCache = field(default_factory=_TokenCache)

    # ------------------------------------------------------------ token
    async def access_token(self) -> str:
        """取微信客服 access_token（带缓存）；未配置或失败返回空串。"""
        if self._token.alive():
            return self._token.value
        secret = self.config.kf_secret_effective
        if not (self.config.corp_id and secret):
            return ""
        try:
            import httpx  # noqa: PLC0415 - 延迟导入，纯本地使用时不必装
        except Exception:  # noqa: BLE001
            log.warning("httpx 不可用，微信客服调用已跳过")
            return ""
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(
                    f"{WX_CORP_API}/cgi-bin/gettoken",
                    params={"corpid": self.config.corp_id, "corpsecret": secret},
                )
                data = resp.json()
        except Exception as err:  # noqa: BLE001 - 网络异常只降级不抛
            log.warning("微信客服 access_token 获取失败：{}", err)
            return ""
        token = str(data.get("access_token") or "")
        if not token:
            log.warning("微信客服 access_token 返回异常：{}", data)
            return ""
        self._token.store(token)
        return token

    # ------------------------------------------------------------ 通用请求
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self.access_token()
        if not token:
            return {}
        try:
            import httpx  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return {}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(
                    f"{WX_CORP_API}{path}",
                    params={"access_token": token},
                    json=body,
                )
                return resp.json() if resp.content else {}
        except Exception as err:  # noqa: BLE001
            log.warning("微信客服接口 {} 请求失败：{}", path, err)
            return {}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = await self.access_token()
        if not token:
            return {}
        try:
            import httpx  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return {}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(
                    f"{WX_CORP_API}{path}",
                    params={**params, "access_token": token},
                )
                return resp.json() if resp.content else {}
        except Exception as err:  # noqa: BLE001
            log.warning("微信客服接口 {} 请求失败：{}", path, err)
            return {}

    # ------------------------------------------------------------ 账号
    async def account_list(self) -> list[dict[str, Any]]:
        """获取客服账号列表（用于自动发现 open_kfid）。"""
        data = await self._get("/cgi-bin/kf/account/list", {"offset": 0, "limit": 100})
        if int(data.get("errcode") or 0) != 0:
            log.warning("微信客服账号列表获取失败：{}", data.get("errmsg"))
            return []
        accounts = data.get("account_list") or []
        return [item for item in accounts if isinstance(item, dict)]

    async def resolve_open_kfid(self) -> str:
        """配置了就用配置的，否则取账号列表第一个。"""
        if self.config.kf_open_kfid:
            return self.config.kf_open_kfid
        accounts = await self.account_list()
        if not accounts:
            return ""
        kfid = str(accounts[0].get("open_kfid") or "")
        if kfid:
            log.info(
                "微信客服未配置 open_kfid，自动使用第一个账号：{}（{}）",
                kfid,
                accounts[0].get("name") or "未命名",
            )
        return kfid

    # ------------------------------------------------------------ 拉消息
    async def sync_msg(
        self,
        *,
        open_kfid: str,
        cursor: str = "",
        token: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        """增量拉取消息。

        ``token`` 来自回调事件（10 分钟内有效）。带 token 调用可避开官方
        对无 token 拉取的严格频率限制，所以回调路径一定要传。
        """
        if not open_kfid:
            log.warning("微信客服 sync_msg 缺少 open_kfid")
            return {}
        body = {
            "cursor": cursor,
            "token": token,
            "limit": limit,
            "voice_format": 0,
            "open_kfid": open_kfid,
        }
        data = await self._post("/cgi-bin/kf/sync_msg", body)
        errcode = int(data.get("errcode") or 0)
        if errcode != 0:
            log.warning(
                "微信客服 sync_msg 失败：errcode={} errmsg={}",
                errcode,
                data.get("errmsg"),
            )
            return {}
        return data

    # ------------------------------------------------------------ 发消息
    async def send_text(
        self,
        external_userid: str,
        text: str,
        *,
        open_kfid: str = "",
        msgid: str = "",
    ) -> bool:
        """给家长发文本。

        返回 True 只代表接口受理；**是否真送达要看 ``msg_send_fail`` 事件**
        （48 小时窗口过期、超过 5 条、用户拒收都在事件里报）。
        """
        if not (external_userid and text):
            return False
        kfid = open_kfid or self.config.kf_open_kfid
        if not kfid:
            log.warning("微信客服 send_msg 缺少 open_kfid（可配 WECHAT_KF_OPEN_KFID）")
            return False
        body: dict[str, Any] = {
            "touser": external_userid,
            "open_kfid": kfid,
            "msgtype": "text",
            "text": {"content": truncate_utf8(text)},
        }
        if msgid:
            body["msgid"] = str(msgid)[:32]
        data = await self._post("/cgi-bin/kf/send_msg", body)
        errcode = int(data.get("errcode") or 0)
        if errcode != 0:
            log.warning(
                "微信客服消息被拒（{}）：errcode={} errmsg={}",
                external_userid,
                errcode,
                data.get("errmsg"),
            )
            return False
        return True

    async def send_welcome(self, welcome_code: str, text: str) -> bool:
        """回复「用户进入会话」事件：家长点开窗口就自动收到这段内容。

        ``welcome_code`` 来自 ``enter_session`` 事件，官方限制：用户过去
        48 小时未收过欢迎语、且未向客服发过消息时才会下发。
        """
        if not (welcome_code and text):
            return False
        body = {
            "code": welcome_code,
            "msgtype": "text",
            "text": {"content": truncate_utf8(text)},
        }
        data = await self._post("/cgi-bin/kf/send_msg_on_event", body)
        errcode = int(data.get("errcode") or 0)
        if errcode != 0:
            log.warning(
                "微信客服欢迎语发送失败：errcode={} errmsg={}",
                errcode,
                data.get("errmsg"),
            )
            return False
        return True

    async def send_menu(self, external_userid: str, head: str, tail: str = "", **kwargs: Any) -> bool:
        """给家长发菜单消息（点一下就回一条文本，省得打字）。

        家长点击后微信会自动回传一条带 ``text.menu_id`` 的文本消息，
        由 :mod:`wechat.kf_service` 统一处理，不需要额外分支。
        """
        if not external_userid:
            return False
        kfid = kwargs.get("open_kfid") or self.config.kf_open_kfid
        if not kfid:
            return False
        items = [
            {"type": "click", "click": {"id": "xueqing", "content": "查学情"}},
            {"type": "click", "click": {"id": "kebiao", "content": "查课时"}},
            {"type": "click", "click": {"id": "bangzhu", "content": "看帮助"}},
        ]
        body = {
            "touser": external_userid,
            "open_kfid": kfid,
            "msgtype": "msgmenu",
            "msgmenu": {
                "head_content": truncate_utf8(head, 1024),
                "list": items,
                "tail_content": truncate_utf8(tail, 1024),
            },
        }
        data = await self._post("/cgi-bin/kf/send_msg", body)
        return int(data.get("errcode") or 0) == 0


_CLIENT = KfClient()


def get_kf_client() -> KfClient:
    return _CLIENT


def reset_kf_client(config: WeChatConfig | None = None) -> KfClient:
    """重建客户端（测试 / 热改配置用）。"""
    global _CLIENT
    _CLIENT = KfClient(config=config or load_config())
    return _CLIENT
