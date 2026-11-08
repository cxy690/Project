# -*- coding: utf-8 -*-
"""临时：验证 uvloop 事件循环下 httpx 连 127.0.0.1:8012 是否失败（用完可删）。"""
from __future__ import annotations

import asyncio
import importlib.util


def spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


async def probe(tag: str) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post("http://127.0.0.1:8012/health")
        print(f"{tag}: OK status={r.status_code}")


def main() -> None:
    print("uvloop installed:", spec("uvloop"))
    print("uvicorn installed:", spec("uvicorn"))
    try:
        asyncio.run(probe("asyncio-loop"))
    except Exception as err:
        print("asyncio-loop FAILED:", type(err).__name__, str(err)[:200])
    if spec("uvloop"):
        import uvloop
        uvloop.install()
        try:
            asyncio.run(probe("uvloop"))
        except Exception as err:
            print("uvloop FAILED:", type(err).__name__, str(err)[:200])


main()
