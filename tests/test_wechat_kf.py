"""微信客服（WeCom KF）测试：游标、截断、拉取分发、回调路由。

和其他微信测试一样**不联网**：httpx 调用全部用假客户端替代，
只验证本地可确定的行为（游标持久化、消息分发、欢迎语、事件路由）。
"""
from __future__ import annotations

import pytest

from wechat import handlers, kf, kf_service
from wechat.config import WeChatConfig
from wechat.crypto import sign_message

TOKEN = "test-token"


class FakeKfClient:
    """假客户端：记录调用，返回预置的 sync_msg 分页。"""

    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = list(pages or [])
        self.sent: list[tuple[str, str]] = []
        self.welcomes: list[tuple[str, str]] = []
        self.sync_calls: list[dict] = []

    async def resolve_open_kfid(self) -> str:
        return "wk-test"

    async def sync_msg(self, **kwargs):
        self.sync_calls.append(kwargs)
        if self.pages:
            return self.pages.pop(0)
        return {"errcode": 0, "has_more": 0, "next_cursor": "", "msg_list": []}

    async def send_text(self, external_userid: str, text: str, **kwargs) -> bool:
        self.sent.append((external_userid, text))
        return True

    async def send_welcome(self, welcome_code: str, text: str) -> bool:
        self.welcomes.append((welcome_code, text))
        return True


def make_config(tmp_path, **overrides) -> WeChatConfig:
    payload = {
        "token": TOKEN,
        "corp_id": "ww-test-corp",
        "kf_secret": "kf-secret",
        "kf_open_kfid": "wk-test",
        "kf_cursor_file": str(tmp_path / "kf_cursor.json"),
        "bind_secret": "8888",
    }
    payload.update(overrides)
    return WeChatConfig(**payload)


@pytest.fixture()
def bindings(tmp_path, monkeypatch):
    path = tmp_path / "bindings.json"
    monkeypatch.setattr(handlers, "BINDINGS_PATH", path)
    return path


# ---------------------------------------------------------------- 配置
def test_kf_enabled_requires_corp_token_and_secret() -> None:
    assert WeChatConfig(corp_id="ww", token="t", kf_secret="s").kf_enabled
    # 没配专用 Secret 时回退应用 Secret
    assert WeChatConfig(corp_id="ww", token="t", app_secret="s").kf_secret_effective == "s"
    # 缺 token 收不到回调事件，视为整体关闭
    assert not WeChatConfig(corp_id="ww", app_secret="s").kf_enabled
    # 缺 corp_id 换不到 token
    assert not WeChatConfig(token="t", kf_secret="s").kf_enabled


# ---------------------------------------------------------------- 游标
def test_cursor_roundtrip_and_isolation(tmp_path) -> None:
    cfg = make_config(tmp_path)
    assert kf.load_cursor("wk-a", cfg) == ""
    kf.save_cursor("wk-a", "cursor-1", cfg)
    kf.save_cursor("wk-b", "cursor-2", cfg)
    assert kf.load_cursor("wk-a", cfg) == "cursor-1"
    assert kf.load_cursor("wk-b", cfg) == "cursor-2"
    assert kf.load_cursor("", cfg) == ""


def test_cursor_survives_broken_file(tmp_path) -> None:
    cfg = make_config(tmp_path)
    path = tmp_path / "kf_cursor.json"
    path.write_text("{not-json", encoding="utf-8")
    assert kf.load_cursor("wk-a", cfg) == ""
    kf.save_cursor("wk-a", "cursor-1", cfg)
    assert kf.load_cursor("wk-a", cfg) == "cursor-1"


# ---------------------------------------------------------------- 文本截断
def test_truncate_utf8_does_not_split_chinese() -> None:
    text = "学" * 1000  # 3000 字节，超过 2048
    out = kf.truncate_utf8(text)
    assert len(out.encode("utf-8")) <= 2048
    assert out == "学" * (2048 // 3)
    # 短文本原样返回
    assert kf.truncate_utf8("学情") == "学情"


def test_fail_type_covers_session_expired() -> None:
    assert "48" in kf.FAIL_TYPES[4]
    assert "5 条" in kf.FAIL_TYPES[6]


# ---------------------------------------------------------------- 消息分发
async def test_text_message_gets_reply(monkeypatch, bindings, tmp_path) -> None:
    cfg = make_config(tmp_path)
    fake = FakeKfClient(
        pages=[{
            "errcode": 0,
            "has_more": 0,
            "next_cursor": "c1",
            "msg_list": [{
                "msgid": "m1",
                "open_kfid": "wk-test",
                "external_userid": "wm-parent",
                "origin": 3,
                "msgtype": "text",
                "text": {"content": "帮助"},
            }],
        }]
    )
    monkeypatch.setattr(kf_service, "get_kf_client", lambda: fake)

    handled = await kf_service.handle_callback("cb-token", "wk-test", cfg)

    assert handled == 1
    assert fake.sent and fake.sent[0][0] == "wm-parent"
    assert "可用指令" in fake.sent[0][1]
    # 游标已落盘，下一次不会重拉
    assert kf.load_cursor("wk-test", cfg) == "c1"
    assert fake.sync_calls[0]["token"] == "cb-token"


async def test_menu_click_is_mapped_to_command(monkeypatch, bindings, tmp_path) -> None:
    cfg = make_config(tmp_path)
    fake = FakeKfClient(
        pages=[{
            "errcode": 0,
            "has_more": 0,
            "next_cursor": "c2",
            "msg_list": [{
                "msgid": "m2",
                "open_kfid": "wk-test",
                "external_userid": "wm-new",
                "origin": 3,
                "msgtype": "text",
                # 家长点「查学情」，微信回传的是显示文案 + menu_id
                "text": {"content": "查学情", "menu_id": "xueqing"},
            }],
        }]
    )
    monkeypatch.setattr(kf_service, "get_kf_client", lambda: fake)

    handled = await kf_service.handle_callback("cb-token", "wk-test", cfg)

    assert handled == 1
    # 映射成「学情」后走绑定校验分支，而不是「没看懂」
    assert "还没有绑定" in fake.sent[0][1]


async def test_enter_session_sends_welcome(monkeypatch, bindings, tmp_path) -> None:
    cfg = make_config(tmp_path)
    fake = FakeKfClient(
        pages=[{
            "errcode": 0,
            "has_more": 0,
            "next_cursor": "c3",
            "msg_list": [{
                "msgid": "m3",
                "origin": 4,
                "msgtype": "event",
                "event": {
                    "event_type": "enter_session",
                    "open_kfid": "wk-test",
                    "external_userid": "wm-new",
                    "welcome_code": "welcome-1",
                },
            }],
        }]
    )
    monkeypatch.setattr(kf_service, "get_kf_client", lambda: fake)

    handled = await kf_service.handle_callback("cb-token", "wk-test", cfg)

    assert handled == 1
    assert fake.welcomes and fake.welcomes[0][0] == "welcome-1"
    assert "学习助手" in fake.welcomes[0][1]


async def test_enter_session_without_welcome_code_is_ignored(monkeypatch, bindings, tmp_path) -> None:
    """48 小时内已发过欢迎语时官方不给 welcome_code，不该报错。"""
    cfg = make_config(tmp_path)
    fake = FakeKfClient(
        pages=[{
            "errcode": 0,
            "has_more": 0,
            "next_cursor": "c4",
            "msg_list": [{
                "msgid": "m4",
                "origin": 4,
                "msgtype": "event",
                "event": {
                    "event_type": "enter_session",
                    "external_userid": "wm-new",
                    "welcome_code": "",
                },
            }],
        }]
    )
    monkeypatch.setattr(kf_service, "get_kf_client", lambda: fake)

    assert await kf_service.handle_callback("cb-token", "wk-test", cfg) == 0
    assert fake.welcomes == []


async def test_staff_message_origin_is_ignored(monkeypatch, bindings, tmp_path) -> None:
    """origin=5 是企业接待人员自己发的，不能再回一遍。"""
    cfg = make_config(tmp_path)
    fake = FakeKfClient(
        pages=[{
            "errcode": 0,
            "has_more": 0,
            "next_cursor": "c5",
            "msg_list": [{
                "msgid": "m5",
                "origin": 5,
                "msgtype": "text",
                "external_userid": "wm-parent",
                "text": {"content": "同学你好"},
            }],
        }]
    )
    monkeypatch.setattr(kf_service, "get_kf_client", lambda: fake)

    assert await kf_service.handle_callback("cb-token", "wk-test", cfg) == 0
    assert fake.sent == []


async def test_disabled_kf_skips_callback(monkeypatch, tmp_path) -> None:
    cfg = WeChatConfig()
    fake = FakeKfClient()
    monkeypatch.setattr(kf_service, "get_kf_client", lambda: fake)
    assert await kf_service.handle_callback("cb-token", "wk-test", cfg) == 0
    assert fake.sync_calls == []


# ---------------------------------------------------------------- 回调路由
def test_kf_event_is_forwarded_to_background(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WECHAT_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret")
    from server.app import app

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "wechat.routes.schedule_kf_callback",
        lambda token, open_kfid="": calls.append((token, open_kfid)),
    )

    client = TestClient(app)
    ts, nonce = "1700000000", "7"
    signature = sign_message(TOKEN, ts, nonce, "")
    xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-test-corp]]></ToUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[kf_msg_or_event]]></Event>"
        "<Token><![CDATA[cb-token]]></Token>"
        "<OpenKfId><![CDATA[wk-test]]></OpenKfId>"
        "</xml>"
    )

    resp = client.post(
        "/wechat/callback",
        params={"signature": signature, "timestamp": ts, "nonce": nonce},
        content=xml.encode("utf-8"),
    )

    assert resp.status_code == 200
    assert resp.text == "success"
    assert calls == [("cb-token", "wk-test")]
