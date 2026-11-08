"""数字人链路测试：分句、流式 TTS、Avatar 引擎抽象与 Supervisor 双链路切换。"""
from __future__ import annotations

import httpx

from config.settings import settings
from core.state.models import AIResponse
from core.state.session import SessionState
from core.supervisor import Supervisor
from models.avatar import HttpAvatar, MockAvatar, get_avatar
from models.base import BaseAvatarClient
from models.tts import MockTTS, split_sentences


# ---------------------------------------------------------------- 分句
def test_split_sentences_chinese() -> None:
    assert split_sentences("你好。我们开始吧！第一步，读题。") == ["你好。", "我们开始吧！", "第一步，读题。"]


def test_split_sentences_empty_and_mixed() -> None:
    assert split_sentences("") == []
    assert split_sentences("hello world") == ["hello world"]
    assert split_sentences("。。。") == []


# ---------------------------------------------------------------- 流式 TTS
async def test_mock_tts_stream_yields_per_sentence() -> None:
    tts = MockTTS()
    chunks = [item async for item in tts.synthesize_stream("你好。今天讲分数。")]
    assert [c[1] for c in chunks] == ["你好。", "今天讲分数。"]
    assert all(c[0] == b"" for c in chunks)


async def test_base_tts_default_stream() -> None:
    """未覆写 stream 的子类走基类默认实现：逐句 synthesize。"""
    from models.base import BaseTTSClient

    class FakeTTS(BaseTTSClient):
        async def synthesize(self, text: str, *, teacher_id: str | None = None) -> bytes:
            return text.encode()

    tts = FakeTTS()
    chunks = [item async for item in tts.synthesize_stream("第一句。第二句。")]
    assert chunks == [("第一句。".encode(), "第一句。"), ("第二句。".encode(), "第二句。")]


# ---------------------------------------------------------------- Avatar 引擎
async def test_mock_avatar_is_puppet_mode() -> None:
    avatar = MockAvatar()
    assert avatar.produces_video is False
    result = await avatar.render(b"audio", text="你好")
    assert result["mode"] == "puppet"


def test_get_avatar_defaults_to_mock(monkeypatch) -> None:
    """未配置引擎（backend=mock）时回退 MockAvatar；断言与 .env 无关。"""
    monkeypatch.setattr(settings, "avatar_backend", "mock")
    get_avatar.cache_clear()
    try:
        assert isinstance(get_avatar(), MockAvatar)
    finally:
        get_avatar.cache_clear()


async def test_http_avatar_posts_and_parses(monkeypatch) -> None:
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"video": "QUJD", "mime": "video/mp4"}

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    avatar = HttpAvatar("http://127.0.0.1:8010/", api_key="k")
    result = await avatar.render(b"abc", text="你好。")
    assert captured["url"] == "http://127.0.0.1:8010/render"
    assert captured["json"]["text"] == "你好。"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert result == {"mode": "video", "text": "你好。", "video": "QUJD", "mime": "video/mp4"}


# ---------------------------------------------------------------- Supervisor 双链路
class _FakeStreamTTS(MockTTS):
    async def synthesize_stream(self, text: str, *, teacher_id: str | None = None):
        for sentence in split_sentences(text):
            yield sentence.encode(), sentence


class _FakeVideoAvatar(BaseAvatarClient):
    produces_video = True

    def __init__(self) -> None:
        self.gestures: list = []          # 记录每句渲染收到的 gesture，供断言

    async def render(self, audio: bytes, *, text: str = "", gesture: str | None = None) -> dict:
        self.gestures.append(gesture)
        return {"mode": "video", "video": "QUJD", "mime": "video/mp4", "text": text}


def _make_supervisor() -> tuple[Supervisor, list]:
    events: list = []
    state = SessionState(session_id="av", student_id="s")
    state.emit = lambda payload: events.append(payload)
    return Supervisor(state), events


async def test_supervisor_puppet_fallback_with_mock_avatar(monkeypatch) -> None:
    # 测试隔离：该用例验证「回退照片木偶」路径，不应受部署 .env 中
    # AVATAR_FALLBACK_PUPPET=false 的影响
    monkeypatch.setattr(settings, "avatar_fallback_puppet", True)
    supervisor, events = _make_supervisor()
    supervisor.tts = MockTTS()  # MockTTS 返回空音频 -> 前端回退朗读
    await supervisor._push_audio(AIResponse(speech="你好，我们来看这道题。"))
    types = [e.get("type") for e in events]
    assert "ai_speak_text" in types
    assert "ai_avatar" not in types
    assert "ai_audio" not in types


async def test_supervisor_video_path_streams_avatar_events() -> None:
    supervisor, events = _make_supervisor()
    supervisor.tts = _FakeStreamTTS()
    supervisor.avatar = _FakeVideoAvatar()
    await supervisor._push_audio(AIResponse(speech="你好。我们开始吧。"))
    avatar_events = [e for e in events if e.get("type") == "ai_avatar"]
    assert [e["payload"]["text"] for e in avatar_events] == ["你好。", "我们开始吧。"]
    assert all(e["payload"]["mode"] == "video" for e in avatar_events)
    assert not [e for e in events if e.get("type") in ("ai_audio", "ai_speak_text")]


async def test_supervisor_video_failure_falls_back_to_puppet(monkeypatch) -> None:
    monkeypatch.setattr(settings, "avatar_fallback_puppet", True)  # 测试隔离，同上

    class _BrokenAvatar(BaseAvatarClient):
        produces_video = True

        async def render(self, audio: bytes, *, text: str = "", gesture: str | None = None) -> dict:
            raise ConnectionError("renderer down")

    supervisor, events = _make_supervisor()
    supervisor.tts = _FakeStreamTTS()
    supervisor.avatar = _BrokenAvatar()
    await supervisor._push_audio(AIResponse(speech="你好。"))
    types = [e.get("type") for e in events]
    # 渲染失败 -> 回退旧链路；_FakeStreamTTS 的 synthesize 仍为空 -> ai_speak_text
    assert "ai_speak_text" in types


async def test_silent_audio_blocks_video_unless_allowed(monkeypatch) -> None:
    """无 TTS 音频时默认不调渲染服务；开启 AVATAR_ALLOW_SILENT_AUDIO 后可用于链路空跑。"""
    supervisor, events = _make_supervisor()
    supervisor.tts = MockTTS()                      # 空音频
    supervisor.avatar = _FakeVideoAvatar()

    await supervisor._push_audio(AIResponse(speech="你好。"))
    assert not [e for e in events if e.get("type") == "ai_avatar"]

    events.clear()
    monkeypatch.setattr(settings, "avatar_allow_silent_audio", True)
    await supervisor._push_audio(AIResponse(speech="你好。我们开始。"))
    assert len([e for e in events if e.get("type") == "ai_avatar"]) == 2


async def test_video_without_audio_track_also_pushes_tts_audio(monkeypatch) -> None:
    """引擎返回无声视频时，额外下发 ai_audio，避免学生只看到画面听不到声音。"""
    monkeypatch.setattr(settings, "avatar_video_has_audio", False)
    supervisor, events = _make_supervisor()
    supervisor.tts = _FakeStreamTTS()
    supervisor.avatar = _FakeVideoAvatar()
    await supervisor._push_audio(AIResponse(speech="你好。"))
    types = [e.get("type") for e in events]
    assert types == ["ai_avatar", "ai_audio"]


async def test_gesture_applied_to_first_sentence_only() -> None:
    """手势只在首句叠加，后续句子回到纯讲解（gesture=None），避免反复做同一动作。"""
    supervisor, _events = _make_supervisor()
    supervisor.tts = _FakeStreamTTS()
    avatar = _FakeVideoAvatar()
    supervisor.avatar = avatar
    await supervisor._push_avatar_video("你好。我们开始吧。", gesture="nod")
    assert avatar.gestures == ["nod", None]


async def test_push_audio_reads_gesture_from_response_meta() -> None:
    """_push_audio 从 AIResponse.meta['gesture'] 取手势并传给首句渲染。"""
    supervisor, _events = _make_supervisor()
    supervisor.tts = _FakeStreamTTS()
    avatar = _FakeVideoAvatar()
    supervisor.avatar = avatar
    await supervisor._push_audio(AIResponse(speech="答对啦。真棒。", meta={"gesture": "nod"}))
    assert avatar.gestures[0] == "nod"
