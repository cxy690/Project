# -*- coding: utf-8 -*-
"""微信客服配置向导：把 CorpID / Secret 写进 ``.env``，避免手改格式出错。

用法：
    python scripts/kf_setup.py                     # 交互式录入（推荐）
    python scripts/kf_setup.py --dry-run           # 只看会写什么，不落盘
    python scripts/kf_setup.py --corp-id ww123 --secret abc --token t1

安全约定：
    * **只改** ``WECHAT_`` 开头的指定 key，其它行（数据库、模型配置等）原样保留；
    * 落盘前先备份成 ``.env.bak-<时间戳>``；
    * 不打印 Secret 明文，只显示前 4 位。

写完会提示下一步跑 ``scripts/kf_selftest.py`` 验证。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

#: 参数名 -> .env 里的键
FIELDS: list[tuple[str, str, str]] = [
    ("corp_id", "WECHAT_CORP_ID", "我的企业 -> 企业信息 -> 企业ID"),
    ("secret", "WECHAT_KF_SECRET", "微信客服 -> API -> Secret（或自建应用 Secret）"),
    ("token", "WECHAT_TOKEN", "回调签名 Token（自己定，后台填同一个；可先留空）"),
    ("aes_key", "WECHAT_ENCODING_AES_KEY", "回调 EncodingAESKey（后台随机生成，43 位）"),
    ("bind_secret", "WECHAT_BIND_SECRET", "家长绑定验证码（班主任发给家长）"),
    ("open_kfid", "WECHAT_KF_OPEN_KFID", "客服账号 ID（wk 开头，可留空自动选第一个）"),
]


def _mask(value: str) -> str:
    text = str(value or "")
    return f"{text[:4]}***" if len(text) > 4 else text


def read_env() -> list[str]:
    if not ENV_PATH.is_file():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def upsert(values: dict[str, str], dry: bool = False) -> list[str]:
    """把 values 写进 .env：已存在的 key 原地替换，新 key 追加到末尾。"""
    lines = read_env()
    out: list[str] = []
    written: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                written.add(key)
                continue
        out.append(line)

    missing = {k: v for k, v in values.items() if k not in written}
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# ===== 微信客服（由 scripts/kf_setup.py 写入）=====")
        for key, value in missing.items():
            out.append(f"{key}={value}")

    if dry:
        return out

    if ENV_PATH.is_file():
        backup = ENV_PATH.parent / f".env.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.write_text(ENV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[OK] 已备份原配置：{backup.name}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return out


def interactive() -> dict[str, str]:
    print("微信客服配置向导（直接回车 = 跳过该项，不写入）\n")
    print("提示：管理后台用浏览器打开 https://work.weixin.qq.com ，")
    print("      客户端左上角「管理企业」也会跳过去。\n")

    values: dict[str, str] = {}
    for attr, key, hint in FIELDS:
        required = attr in {"corp_id", "secret"}
        prompt = f"{hint}\n  {key}{'（必填）' if required else ''}: "
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未写入任何内容。")
            raise SystemExit(1)
        if not raw:
            if required:
                print(f"  ! {key} 是必填项，跳过会导致后面自检失败。")
            continue
        values[key] = raw
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="微信客服配置向导")
    parser.add_argument("--corp-id", default="", help="WECHAT_CORP_ID")
    parser.add_argument("--secret", default="", help="WECHAT_KF_SECRET")
    parser.add_argument("--token", default="", help="WECHAT_TOKEN")
    parser.add_argument("--aes-key", default="", help="WECHAT_ENCODING_AES_KEY")
    parser.add_argument("--bind-secret", default="", help="WECHAT_BIND_SECRET")
    parser.add_argument("--open-kfid", default="", help="WECHAT_KF_OPEN_KFID")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    args = parser.parse_args()

    cli_values = {
        "WECHAT_CORP_ID": args.corp_id,
        "WECHAT_KF_SECRET": args.secret,
        "WECHAT_TOKEN": args.token,
        "WECHAT_ENCODING_AES_KEY": args.aes_key,
        "WECHAT_BIND_SECRET": args.bind_secret,
        "WECHAT_KF_OPEN_KFID": args.open_kfid,
    }
    values = {k: v.strip() for k, v in cli_values.items() if v.strip()}
    if not values:
        values = interactive()

    if not values:
        print("没有录入任何内容。")
        return 1

    if "WECHAT_CORP_ID" not in values or "WECHAT_KF_SECRET" not in values:
        print("\n[WARN] CorpID 或 Secret 缺失，自检会停在第 1 步。可以先写，之后再补。")

    print("\n即将写入：")
    for key, value in values.items():
        print(f"  {key}={_mask(value)}")

    upsert(values, dry=args.dry_run)
    if args.dry_run:
        print("\n[dry-run] 未修改 .env")
        return 0

    print(f"\n[OK] 已写入 {ENV_PATH}")
    print("下一步：python scripts/kf_selftest.py")
    print("（脚本会读 .env，不用重启应用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
