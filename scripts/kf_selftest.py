# -*- coding: utf-8 -*-
"""微信客服接入自检：配好 ``.env`` 后先跑这个，别直接上线等家长消息。

用法：
    python scripts/kf_selftest.py                  # 只体检，不给家长发消息
    python scripts/kf_selftest.py --send wmXXXXXX  # 给指定家长发一条测试消息
    python scripts/kf_selftest.py --no-pull        # 不拉取消息（避免打扰游标）

检查顺序：
1. 环境变量是否齐全（缺哪项、去后台哪里拿）；
2. 能否换到 access_token（CorpID / Secret 是否正确）；
3. 能否列出客服账号（自建应用有没有加进「可调用接口的应用」）——
   这一步失败最常见，报错通常是 errcode=48002 或 60020；
4. 游标位置 + 最近 3 天的消息（只打印摘要，**不回复家长**）。

注意：脚本会真实调用企业微信接口（只读为主），不会改动线上游标。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from wechat.config import load_config  # noqa: E402
from wechat.kf import FAIL_TYPES, get_kf_client, load_cursor  # noqa: E402


def _ok(text: str) -> None:
    print(f"[OK]   {text}")


def _miss(text: str) -> None:
    print(f"[MISS] {text}")


def _fail(text: str) -> None:
    print(f"[FAIL] {text}")


def check_config() -> bool:
    cfg = load_config()
    print("=== 1. 配置检查 ===")
    # 只有这两项是自检的硬前提：换 token 必须 CorpID + Secret
    blocking: list[str] = []
    if not cfg.corp_id:
        blocking.append("WECHAT_CORP_ID（企微后台 -> 我的企业 -> 企业信息）")
    if not cfg.kf_secret_effective:
        blocking.append("WECHAT_KF_SECRET（企微后台 -> 微信客服 -> API 获取）")
    for item in blocking:
        _miss(item)

    # 下面几项只影响「家长发消息后自动回复」，不影响主动发消息的自检
    if not cfg.token:
        _miss("WECHAT_TOKEN 未配：自检可以跑通，但回调会 404（上线前必须补）")
    if not cfg.encoding_aes_key:
        _miss("WECHAT_ENCODING_AES_KEY 未配：只能明文模式，而企微后台默认要求加密模式")
    if not cfg.bind_secret:
        _miss("WECHAT_BIND_SECRET 未配：「绑定」指令会被禁用，家长无法关联学生")

    if blocking:
        print("\n核心配置不全（CorpID / Secret），后续接口检查跳过。")
        return False
    _ok("CorpID / 微信客服 Secret 已配置")
    return True


async def check_token() -> bool:
    print("\n=== 2. access_token ===")
    client = get_kf_client()
    token = await client.access_token()
    if not token:
        _fail("拿不到 access_token，检查 WECHAT_CORP_ID / WECHAT_KF_SECRET 是否正确、服务器能否访问 qyapi.weixin.qq.com")
        return False
    _ok(f"已获取（{_mask(token)}，长度 {len(token)}）")
    return True


async def check_accounts() -> str:
    print("\n=== 3. 客服账号 ===")
    client = get_kf_client()
    accounts = await client.account_list()
    if not accounts:
        _fail("列不出客服账号。最常见原因：自建应用没加进「微信客服 -> 可调用接口的应用」")
        return ""
    for item in accounts:
        print(f"       open_kfid={item.get('open_kfid')}  name={item.get('name')}")
    cfg = load_config()
    resolved = await client.resolve_open_kfid()
    if cfg.kf_open_kfid:
        found = {str(item.get("open_kfid")) for item in accounts}
        if cfg.kf_open_kfid in found:
            _ok(f"WECHAT_KF_OPEN_KFID 配置有效：{cfg.kf_open_kfid}")
        else:
            _fail(f"WECHAT_KF_OPEN_KFID={cfg.kf_open_kfid} 不在账号列表里")
    _ok(f"实际使用 open_kfid={resolved}")
    return resolved


async def check_messages(open_kfid: str, pull: bool) -> str:
    """拉一次消息，返回最近一条「家长发的」消息的 external_userid。"""
    print("\n=== 4. 消息与游标 ===")
    cfg = load_config()
    cursor = load_cursor(open_kfid, cfg)
    print(f"       当前游标：{cursor or '（空，会从 3 天内最早的消息开始拉）'}")

    if not pull:
        print("       --no-pull：跳过拉取")
        return ""

    client = get_kf_client()
    data = await client.sync_msg(open_kfid=open_kfid, cursor=cursor)
    if not data:
        _fail("sync_msg 调用失败（看上面的日志）")
        return ""

    messages = data.get("msg_list") or []
    print(f"       本次拉到 {len(messages)} 条，has_more={data.get('has_more')}")
    origin_names = {3: "家长发的", 4: "系统事件", 5: "接待人员发的"}
    last_parent = ""
    for message in messages[:10]:
        origin = int(message.get("origin") or 0)
        external = str(message.get("external_userid") or "")
        if origin == 3 and external:
            last_parent = external
        # 这里打完整 id：自检就是要拿它调 --send（是自己的 id，不是别人的）
        print(
            f"       - {origin_names.get(origin, origin)}"
            f" msgtype={message.get('msgtype')}"
            f" 家长={external or '（事件消息无此字段）'}"
        )
    if messages:
        _ok("接口可用。这次拉取不写游标，正式处理走回调")
    elif not cursor:
        print("       还没有消息。先在微信里给客服发一句话，再跑一次这个脚本。")
    return last_parent


async def send_test(external_userid: str, open_kfid: str) -> None:
    print("\n=== 5. 发送测试消息 ===")
    client = get_kf_client()
    text = "【测试】学习助手已接入，收到这条说明发送通道正常。"
    ok = await client.send_text(external_userid, text, open_kfid=open_kfid)
    if ok:
        _ok("接口已受理。**受理不等于送达**：若家长 48 小时内没发过消息，会通过 msg_send_fail 事件报 fail_type=4")
        print("       失败类型对照：" + "；".join(f"{k}={v}" for k, v in sorted(FAIL_TYPES.items())))
    else:
        _fail("发送被拒，看上面的 errcode")


async def main() -> int:
    parser = argparse.ArgumentParser(description="微信客服接入自检")
    parser.add_argument("--send", default="", help="给指定 external_userid 发一条测试消息")
    parser.add_argument(
        "--send-last",
        action="store_true",
        help="发给最近一条家长消息的发送者（自己扫码试的时候用这个）",
    )
    parser.add_argument("--no-pull", action="store_true", help="不拉取消息")
    args = parser.parse_args()

    if not check_config():
        return 1
    if not await check_token():
        return 1
    open_kfid = await check_accounts()
    if not open_kfid:
        return 1
    last_parent = await check_messages(open_kfid, pull=not args.no_pull)
    target = args.send or (last_parent if args.send_last else "")
    if args.send_last and not target:
        _fail("--send-last 没找到可发对象：先在微信里给客服发一句话，且不要加 --no-pull")
    elif target:
        await send_test(target, open_kfid)

    print("\n体检结束。回调地址记得填：https://<固定域名>/wechat/callback")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
