# -*- coding: utf-8 -*-
"""云端复现：主应用路径下的 avatar.render 调用（临时脚本，用完可删）。"""
from __future__ import annotations

import asyncio
import io
import wave


async def main() -> None:
    from models.avatar import get_avatar

    av = get_avatar()
    print("avatar:", type(av).__name__)
    print("api_base:", getattr(av, "api_base", None))
    print("timeout:", getattr(av, "timeout", None))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)  # 0.5s 静音
    audio = buf.getvalue()

    try:
        r = await av.render(audio, text="复现测试", teacher_id="math")
        print("render ok:", str(r)[:200])
    except Exception:
        import traceback
        traceback.print_exc()


asyncio.run(main())
