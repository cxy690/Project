# -*- coding: utf-8 -*-
"""临时：直测当前 TTS 引擎类型与合成结果（用完可删）。"""
from __future__ import annotations

import asyncio
import traceback


async def main() -> None:
    from config import settings
    from models.tts import get_tts

    print("tts_provider:", settings.tts_provider)
    print("cosyvoice_api_base:", settings.cosyvoice_api_base)
    print("doubao_api_base:", settings.doubao_api_base)
    tts = get_tts()
    print("tts engine:", type(tts).__name__)
    for attr in ("clone", "edge"):
        eng = getattr(tts, attr, None)
        if eng is not None:
            print(f"{attr} engine:", type(eng).__name__,
                  getattr(eng, "api_base", "") or "")
    try:
        audio = await tts.synthesize("同学们好，今天我们来学习数轴。", teacher_id="math")
        print("synthesize ok, bytes:", len(audio or b""))
    except Exception:
        traceback.print_exc()


asyncio.run(main())
