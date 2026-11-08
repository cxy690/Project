"""微信客服机器人测试：签名、加解密、消息处理、回调门禁。

不真的调用微信接口：只测本地可验证的部分（验签、解密、绑定、回复文案）。
"""
from __future__ import annotations

import base64
import json

import pytest

from wechat import handlers
from wechat.crypto import (
    WeChatCryptoError,
    decrypt_message,
    encrypt_message,
    sign_message,
    verify_msg_signature,
    verify_signature,
)

TOKEN = "test-token"
# 43 位 = 32 字节密钥的 base64 去掉尾部 '='（微信 EncodingAESKey 的规定形式）
AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("=")


# ---------------------------------------------------------------- 签名
def test_verify_signature_ok_and_bad() -> None:
    ts, nonce = "1700000000", "12345"
    good = sign_message(TOKEN, ts, nonce, "")  # 与明文签名同一套 sha1
    # 明文签名是 sha1(sort(token,timestamp,nonce))，与带空 encrypt 等价
    assert verify_signature(TOKEN, ts, nonce, sign_message(TOKEN, ts, nonce, ""))
    assert not verify_signature(TOKEN, ts, nonce, "deadbeef")
    assert not verify_signature("", ts, nonce, good)


def test_verify_msg_signature_requires_encrypt() -> None:
    ts, nonce, encrypt = "1700000000", "999", "CIPHERTEXT"
    sig = sign_message(TOKEN, ts, nonce, encrypt)
    assert verify_msg_signature(TOKEN, ts, nonce, encrypt, sig)
    assert not verify_msg_signature(TOKEN, ts, nonce, encrypt + "x", sig)
    assert not verify_msg_signature(TOKEN, ts, nonce, "", sig)


# ---------------------------------------------------------------- 加解密
def test_aes_roundtrip() -> None:
    pytest.importorskip("cryptography")
    plain = "<xml><Content><![CDATA[学情]]></Content></xml>"
    encrypt = encrypt_message(AES_KEY, plain, "wx123")
    assert decrypt_message(AES_KEY, encrypt, "wx123") == plain


def test_decrypt_rejects_wrong_receiver() -> None:
    pytest.importorskip("cryptography")
    encrypt = encrypt_message(AES_KEY, "<xml/>", "wx-corp")
    with pytest.raises(WeChatCryptoError):
        decrypt_message(AES_KEY, encrypt, "another-corp")


def test_bad_aes_key_length() -> None:
    with pytest.raises(WeChatCryptoError):
        decrypt_message("short", "AAAA")


# ---------------------------------------------------------------- 消息处理
@pytest.fixture()
def bindings(tmp_path, monkeypatch):
    path = tmp_path / "bindings.json"
    monkeypatch.setattr(handlers, "BINDINGS_PATH", path)
    return path


def test_bind_and_query_requires_binding(bindings) -> None:
    assert "还没有绑定" in handlers.handle_text("open-1", "学情")
    assert handlers.bind("open-1", "stu-1")
    assert handlers.bound_student("open-1") == "stu-1"
    assert handlers.open_ids_of("stu-1") == ["open-1"]
    assert handlers.unbind("open-1")
    assert handlers.bound_student("open-1") == ""


def test_bind_command_checks_secret(bindings, monkeypatch) -> None:
    monkeypatch.setenv("WECHAT_BIND_SECRET", "8888")
    from wechat.config import load_config

    cfg = load_config()
    reply = handlers.handle_text("open-2", "绑定 stu-2 0000", cfg)
    assert "验证码不正确" in reply
    assert handlers.bound_student("open-2") == ""

    reply = handlers.handle_text("open-2", "绑定 stu-2 8888", cfg)
    assert "绑定成功" in reply
    assert handlers.bound_student("open-2") == "stu-2"


def test_bind_disabled_without_secret(bindings) -> None:
    from wechat.config import WeChatConfig

    reply = handlers.handle_text("open-3", "绑定 stu-3 1234", WeChatConfig())
    assert "未开启" in reply


def test_help_and_unknown(bindings) -> None:
    assert "可用指令" in handlers.handle_text("open-4", "帮助")
    assert "没看懂" in handlers.handle_text("open-4", "随便说点什么")


def test_lesson_text_contains_core_metrics() -> None:
    from core.learning_stats.metrics import compute_metrics
    from core.learning_stats.models import EventKind, LessonEvent

    t = 1_700_000_000.0
    events = [
        LessonEvent(t, EventKind.SESSION_START.value, {}),
        LessonEvent(t + 60, EventKind.TEACHER_QUESTION.value, {}),
        LessonEvent(t + 75, EventKind.STUDENT_TEXT.value, {"latency": 15}),
        LessonEvent(t + 1200, EventKind.SESSION_END.value, {}),
    ]
    metrics = compute_metrics(
        lesson_id="L", student_id="stu", subject="数学", grade="四年级",
        events=events, planned_seconds=2400,
    )
    text = handlers.lesson_text(metrics)
    assert "数学" in text and "完课度" in text and "掌握度" in text


# ---------------------------------------------------------------- 回调路由
def test_callback_disabled_without_token(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WECHAT_TOKEN", "")
    monkeypatch.setenv("WECHAT_APP_SECRET", "")
    from server.app import app

    client = TestClient(app)
    assert client.get("/wechat/callback", params={"echostr": "hi"}).status_code == 404


def test_callback_verifies_signature(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WECHAT_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret")
    from server.app import app

    client = TestClient(app)
    ts, nonce = "1700000000", "42"
    good = sign_message(TOKEN, ts, nonce, "")
    ok = client.get("/wechat/callback", params={
        "signature": good, "timestamp": ts, "nonce": nonce, "echostr": "echo-123",
    })
    assert ok.status_code == 200 and ok.text == "echo-123"

    bad = client.get("/wechat/callback", params={
        "signature": "bad", "timestamp": ts, "nonce": nonce, "echostr": "echo-123",
    })
    assert bad.status_code == 403


def test_corp_url_verify_decrypts_echostr(monkeypatch) -> None:
    """企业微信加密模式的 URL 验证：验签 + 解密，返回明文而不是密文。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WECHAT_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret")
    monkeypatch.setenv("WECHAT_CORP_ID", "ww-test-corp")
    monkeypatch.setenv("WECHAT_ENCODING_AES_KEY", AES_KEY)
    from server.app import app

    client = TestClient(app)
    ts, nonce = "1700000000", "42"
    echostr = encrypt_message(AES_KEY, "echo-456", "ww-test-corp")
    good = sign_message(TOKEN, ts, nonce, echostr)

    ok = client.get("/wechat/callback", params={
        "msg_signature": good, "timestamp": ts, "nonce": nonce, "echostr": echostr,
    })
    assert ok.status_code == 200 and ok.text == "echo-456"

    bad = client.get("/wechat/callback", params={
        "msg_signature": "bad", "timestamp": ts, "nonce": nonce, "echostr": echostr,
    })
    assert bad.status_code == 403
