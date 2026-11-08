"""微信客服机器人配置（全部走环境变量，不写死、不入库）。

支持三种接法，按需要开启，**没配就自动关闭对应能力**（不影响主应用启动）：

1. **群机器人 webhook**（最省事）：企业微信群 -> 群机器人 -> 复制 webhook 地址，
   配 ``WECHAT_WEBHOOK_URL``。只能发消息，适合「课后学情通知 / 周报」。
2. **企业微信应用回调**（可收可发）：配 ``WECHAT_CORP_ID`` / ``WECHAT_AGENT_ID`` /
   ``WECHAT_APP_SECRET`` / ``WECHAT_TOKEN`` / ``WECHAT_ENCODING_AES_KEY``，
   适合家长在会话里查学情。
3. **公众号回调**（可收可发）：配 ``WECHAT_TOKEN`` / ``WECHAT_APP_SECRET`` /
   ``WECHAT_ENCODING_AES_KEY`` / ``WECHAT_APP_ID``。

安全约定：
* 只有配了 ``WECHAT_TOKEN`` 才启用回调路由，否则 ``/wechat/callback`` 直接 404；
* 家长查询学情必须先在消息里绑定 ``学生ID``，且绑定需要 ``WECHAT_BIND_SECRET``
  （避免群里任何人报一个学生名字就能查别人孩子的数据）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _bootstrap_env() -> None:
    """把 ``.env`` 里的 ``WECHAT_*`` 补进 ``os.environ``（已存在的不覆盖）。

    为什么需要：``config/settings.py`` 用 pydantic-settings 读 ``.env``，但它
    只把值装进 Settings 对象，**不会写回进程环境**；而本模块和路由都按
    ``os.getenv`` 取值 —— 少了这一步，``.env`` 里配了也永远读不到（实测
    2026-10-20：回调一直 404，查进程 environ 里没有 WECHAT_*）。
    用 ``setdefault`` 保证容器/系统级环境变量优先于文件。
    """
    if not _ENV_FILE.is_file():
        return
    try:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        if key.startswith("WECHAT_"):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_bootstrap_env()


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


@dataclass(frozen=True)
class WeChatConfig:
    """微信接入配置（进程启动时读一次，改环境变量需重启）。"""

    # 通用
    token: str = ""                  # 回调签名 token
    encoding_aes_key: str = ""       # 回调消息加解密 key（43 位）
    app_id: str = ""                 # 公众号 AppID
    app_secret: str = ""             # 公众号 / 企微应用 Secret
    # 企业微信
    corp_id: str = ""
    agent_id: str = ""
    webhook_url: str = ""            # 群机器人 webhook（只发不收）
    # 微信客服（WeCom KF）：家长扫码即可会话，不用关注公众号、不用加好友
    kf_secret: str = ""              # 微信客服专用 Secret（留空回退 app_secret）
    kf_open_kfid: str = ""           # 默认客服账号 ID（留空自动取账号列表第一个）
    kf_cursor_file: str = ""         # 拉取游标落盘路径（留空用 data/wechat/kf_cursor.json）
    # 家长绑定
    bind_secret: str = ""
    # 通知策略
    notify_lesson_end: bool = True   # 课后是否推送学情
    notify_weekly: bool = False      # 是否推送周报（需外部定时触发）
    notify_daily: bool = True        # 每天定点推「当日学情」（当天有课才推）
    push_hour: int = 22              # 每日推送时刻（0-23，默认 22 点）
    push_minute: int = 0             # 每日推送分钟（0-59，默认 0 分；配合 push_hour 可精确到 20:30）
    report_base_url: str = ""        # 报告页对外地址（固定域名），留空则消息里不带链接
    staff_chat_id: str = ""          # 预警转人工的会话 id（为空则不发）
    min_lesson_seconds: int = 300    # 有效学习不足这个时长不推（断线重连的碎片课）
    class_weekly_min_students: int = 3  # 班级周报展示平均分的最少上课人数（隐私下限）

    @property
    def callback_enabled(self) -> bool:
        """是否启用回调（没有 token 就没人能验签，直接关闭）。"""
        return bool(self.token and self.app_secret)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url)

    @property
    def bind_enabled(self) -> bool:
        return bool(self.bind_secret)

    @property
    def is_corp(self) -> bool:
        return bool(self.corp_id)

    @property
    def kf_secret_effective(self) -> str:
        """微信客服换 token 用的 Secret：优先专用 Secret，回退应用 Secret。"""
        return self.kf_secret or self.app_secret

    @property
    def kf_enabled(self) -> bool:
        """微信客服是否可用。

        三个都要：``corp_id``（换 token）、``token``（回调验签，没它收不到事件）、
        Secret（换 token）。缺任一项就整体关闭，不影响主应用。
        """
        return bool(self.corp_id and self.token and self.kf_secret_effective)


def load_config() -> WeChatConfig:
    """从环境变量读取配置。"""
    return WeChatConfig(
        token=_env("WECHAT_TOKEN"),
        encoding_aes_key=_env("WECHAT_ENCODING_AES_KEY"),
        app_id=_env("WECHAT_APP_ID"),
        app_secret=_env("WECHAT_APP_SECRET"),
        corp_id=_env("WECHAT_CORP_ID"),
        agent_id=_env("WECHAT_AGENT_ID"),
        webhook_url=_env("WECHAT_WEBHOOK_URL"),
        kf_secret=_env("WECHAT_KF_SECRET"),
        kf_open_kfid=_env("WECHAT_KF_OPEN_KFID"),
        kf_cursor_file=_env("WECHAT_KF_CURSOR_FILE"),
        bind_secret=_env("WECHAT_BIND_SECRET"),
        notify_lesson_end=_env("WECHAT_NOTIFY_LESSON_END", "true").lower() in {"1", "true", "yes"},
        notify_weekly=_env("WECHAT_NOTIFY_WEEKLY", "false").lower() in {"1", "true", "yes"},
        notify_daily=_env("WECHAT_NOTIFY_DAILY", "true").lower() in {"1", "true", "yes"},
        push_hour=min(23, max(0, _int_env("WECHAT_PUSH_HOUR", 22))),
        push_minute=min(59, max(0, _int_env("WECHAT_PUSH_MINUTE", 0))),
        report_base_url=_env("WECHAT_REPORT_BASE_URL").rstrip("/"),
        staff_chat_id=_env("WECHAT_STAFF_CHAT_ID"),
        min_lesson_seconds=_int_env("WECHAT_MIN_LESSON_SECONDS", 300),
        class_weekly_min_students=_int_env("WECHAT_CLASS_WEEKLY_MIN_STUDENTS", 3),
    )


def _int_env(name: str, default: int) -> int:
    """读整数环境变量；脏值回退默认值（配置写错不能让推送起不来）。"""
    raw = _env(name)
    try:
        return max(0, int(raw)) if raw else default
    except ValueError:
        return default


#: 进程级配置（路由与通知共用）
CONFIG = load_config()
