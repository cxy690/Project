# -*- coding: utf-8 -*-
"""临时：经 WebSocket 走真实会话，验证数字人渲染是否恢复（用完可删）。

流程：连 127.0.0.1:8001/ws -> init(math) -> ready -> 发一句文本提问 ->
收事件直到出现 ai_avatar（渲染成功）或超时。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

try:
    import websockets
except ImportError:
    print("NO_WEBSOCKETS_LIB: pip install websockets")
    sys.exit(3)

URL = "ws://127.0.0.1:8001/ws"


async def main() -> None:
    async with websockets.connect(URL, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "init", "student_id": "diag-render", "subject": "math", "teacher": "math",
        }, ensure_ascii=False))

        # 等 ready（最多 8s）
        ready = False
        t0 = time.time()
        while time.time() - t0 < 8:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            except asyncio.TimeoutError:
                break
            if msg.get("type") == "ready":
                ready = True
                break
        print("ready:", ready, flush=True)
        if not ready:
            return

        await ws.send(json.dumps({"type": "text", "text": "什么是数轴？用一句话介绍"}, ensure_ascii=False))

        got_avatar = False
        got_chat = False
        t0 = time.time()
        while time.time() - t0 < 150:
            remain = 150 - (time.time() - t0)
            if remain <= 0:
                break
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remain))
            except asyncio.TimeoutError:
                break
            t = msg.get("type")
            p = msg.get("payload") or {}
            if t == "ai_avatar":
                got_avatar = True
                print("ai_avatar: mode=%s stream_url=%s video_len=%s"
                      % (p.get("mode"), str(p.get("stream_url"))[:90],
                         len(p.get("video") or "")), flush=True)
            elif t == "chat_message":
                got_chat = True
                print("chat_message[%s]: %s" % (p.get("role"), str(p.get("text"))[:50]), flush=True)
            elif t == "ai_audio":
                print("ai_audio: len=%s" % len(p.get("audio") or p.get("data") or ""), flush=True)
        print("RESULT got_avatar=%s got_chat=%s" % (got_avatar, got_chat), flush=True)
        await ws.close()


asyncio.run(main())
