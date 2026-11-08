"""e2e 脚本纯函数单测（Markdown 清洗 / 音频魔数）。"""
from __future__ import annotations

from scripts.e2e_test import _audio_ext, clean_markdown


def test_clean_removes_code_block() -> None:
    assert clean_markdown("```\ncode\n```hello") == "hello"


def test_clean_removes_inline_code_and_emphasis() -> None:
    assert clean_markdown("**加粗**和`行内`") == "加粗和行内"


def test_clean_unwraps_links() -> None:
    assert clean_markdown("[链接](http://x)文字") == "链接文字"


def test_clean_strips_headers_and_lists() -> None:
    assert clean_markdown("# 标题\n正文") == "标题 正文"
    assert clean_markdown("- 甲\n- 乙") == "甲 乙"


def test_audio_ext_by_magic() -> None:
    assert _audio_ext(b"RIFF....WAVE") == "wav"
    assert _audio_ext(b"ID3\x03\x00") == "mp3"
    assert _audio_ext(b"\x00\x00\x00\x00") == "mp3"
