# -*- coding: utf-8 -*-
"""微信推送接入自检：配好群之后先跑这个，别等下课才发现发不出去。

用法：
    python scripts/wechat_selftest.py                    # 体检（不发任何消息）
    python scripts/wechat_selftest.py --preview          # 离线预览四种推送文案（不发）
    python scripts/wechat_selftest.py --send g_class_3a  # 往指定群发一条测试消息
    python scripts/wechat_selftest.py --sample g_class_3a # 往指定群发真实样例（2 条，按群性质选）
    python scripts/wechat_selftest.py --no-tcp           # 跳过网络连通性检查

检查顺序：
1. 群配置 ``data/wechat/groups.json``：能不能解析、每个群的性质（parent/class）、
   学生数、webhook 是否指向企业微信官方域名；
2. 环境开关：课后 / 周报是否开启、碎片课门槛、班级周报样本下限；
3. 数据面：有多少学生有学情记录、有多少家长绑定了微信（决定私聊能不能送达）；
4. 网络：服务器能否连上 ``qyapi.weixin.qq.com:443``（云端最常见的问题）；
5. 可选 ``--send``：真发一条测试消息，打印企业微信返回的 errcode；
   ``--sample``：真发学情样例（班级群只发「提示 + 班级周报」，不发任何分数）。

注意：``--send`` / ``--sample`` 会真的发消息到群里（群里所有人可见），
只在确认过群之后用。测试群请只拉企业成员 —— 含微信用户的群是**外部群**，
根本加不了「消息推送」，拿不到 webhook。
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from wechat import groups, handlers  # noqa: E402
from wechat.config import load_config  # noqa: E402

QY_HOST = "qyapi.weixin.qq.com"


def _ok(text: str) -> None:
    print(f"[OK]   {text}")


def _warn(text: str) -> None:
    print(f"[WARN] {text}")


def _miss(text: str) -> None:
    print(f"[MISS] {text}")


def _mask(webhook: str) -> str:
    """webhook 只显示域名与 key 的首尾，避免日志/截图泄露完整 key。"""
    key = webhook.split("key=", 1)[-1] if "key=" in webhook else webhook
    tail = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
    return f"{QY_HOST}/cgi-bin/webhook/send?key={tail}"


def check_groups() -> bool:
    print("=== 1. 群配置 ===")
    path = groups.GROUPS_PATH
    if not path.is_file():
        _miss(f"没有 {path}。复制 wechat/groups.example.json 过去，填 webhook 与学生 id")
        return False

    loaded = groups.load_groups()
    if not loaded:
        _miss("群配置存在但解析不出任何可用群（检查 id / webhook 是否填了）")
        return False

    for group in loaded:
        kind = "班级群（不发单个学生分数）" if group.is_class else "一对一家校群（可发明细）"
        _ok(
            f"{group.id} / {group.name}  {kind}  学生 {len(group.student_ids)} 人  "
            f"课后={'开' if group.lesson_end else '关'} 周报={'开' if group.weekly else '关'}"
        )
        print(f"       webhook: {_mask(group.webhook)}")
        if QY_HOST not in group.webhook:
            _warn("webhook 不是企业微信官方域名，请确认是可信服务（群机器人 key 等于发消息权限）")
    return True


def check_config() -> None:
    print("\n=== 2. 环境开关 ===")
    cfg = load_config()
    _ok(f"课后推送：{'开' if cfg.notify_lesson_end else '关'}（WECHAT_NOTIFY_LESSON_END）")
    if not cfg.notify_weekly:
        _warn("周报推送是关闭的（WECHAT_NOTIFY_WEEKLY=false），脚本需 --force 或改配置")
    _ok(f"有效学习门槛：{cfg.min_lesson_seconds} 秒（不足不推）")
    _ok(f"班级周报平均分样本下限：{cfg.class_weekly_min_students} 人")
    if cfg.webhook_enabled:
        _warn("WECHAT_WEBHOOK_URL 也配了：只要 groups.json 存在，它不会再被使用（避免明细广播）")


def check_data() -> None:
    print("\n=== 3. 数据面 ===")
    from core.learning_stats import list_students  # noqa: PLC0415

    students = list_students(limit=1000)
    if students:
        _ok(f"有学情记录的学生：{len(students)} 人")
    else:
        _warn("还没有任何学情记录：先上一节课，下课才会结算并产生报告")

    bindings_path = handlers.BINDINGS_PATH
    try:
        bound = json_count(bindings_path)
    except Exception:  # noqa: BLE001
        bound = 0
    if bound:
        _ok(f"已绑定微信的家长：{bound} 个 open_id（私聊推送可用）")
    else:
        _warn(
            f"没有家长绑定（{bindings_path}）：私聊发不出去，只能靠群推送；"
            "家长绑定需要先接通微信客服/公众号回调"
        )

    state = handlers.BINDINGS_PATH.parent / "pushed.json"
    if state.is_file():
        _ok(f"推送去重记录：{json_count(state)} 条（同一节课不会重复发）")


def json_count(path: Path) -> int:
    import json  # noqa: PLC0415

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0
    if isinstance(data.get("keys"), dict):
        return len(data["keys"])
    return len(data)


def check_tcp() -> bool:
    print("\n=== 4. 网络连通 ===")
    try:
        with socket.create_connection((QY_HOST, 443), timeout=5):
            _ok(f"可以连上 {QY_HOST}:443")
            return True
    except OSError as err:
        _miss(f"连不上 {QY_HOST}:443（{err}）—— 云端出网被限，webhook 发不出去")
        return False


def _sample_lessons(count: int = 3) -> list:
    """造几节课的样例指标（**全是假数据**，id 以 ``test-`` 开头便于辨认）。"""
    from core.learning_stats.models import LessonMetrics  # noqa: PLC0415

    now = time.time()
    out = []
    for index in range(count):
        start = now - 3600 * (index + 1)
        out.append(
            LessonMetrics(
                lesson_id=f"TEST-{index + 1}",
                student_id=f"test-stu-{index + 1}",
                subject="数学",
                grade="初二",
                stage="初中",
                started_at=start,
                ended_at=start + 2100,
                planned_seconds=2400,
                effective_seconds=2100,
                completion_score=92.0 - index * 4,
                mastery_score=78.0 - index * 3,
                mastery_level="良好",
                interaction_rate=0.63 - index * 0.05,
                quiz_total=6,
                quiz_first_correct=5,
                flags=["二次函数顶点式仍靠公式硬套", "审题漏条件"],
            )
        )
    return out


def build_previews() -> list[tuple[str, str, str]]:
    """四种推送文案的样例：``[(标题, 适用说明, 正文)]``。

    走的是真实模板函数，不是另写一份 —— 预览和线上发出去的必须是同一段代码。
    """
    from unittest import mock  # noqa: PLC0415

    lessons = _sample_lessons(3)
    first = lessons[0]
    week_label = "（本周一 ~ 今天）"
    # 学生周报内部要读存储，这里用样例课次顶掉，避免预览依赖真实上课数据
    with mock.patch("core.learning_stats.list_lessons", lambda **kw: lessons[:1]):
        student_weekly = handlers.weekly_text(
            "test-stu-1", since=0.0, until=time.time(), label=week_label
        )
    return [
        ("课后明细", "一对一家校群 / 家长私聊专用；群里有家长时严禁发这条",
         handlers.lesson_text(first)),
        ("课后提示", "有家长的班级群专用；不含任何分数",
         handlers.lesson_notice_text(first, label="小明", show_name=True)),
        ("学生周报", "一对一家校群 / 家长私聊专用",
         student_weekly),
        ("班级周报概览", "有家长的班级群专用；只给聚合值，不点名",
         handlers.class_weekly_text("初二A班（样例）", week_label, lessons, min_students=3)),
    ]


def preview() -> None:
    """离线打印四种文案，不发任何消息（没有 webhook 也能跑）。"""
    print("\n=== 文案预览（不发送）===")
    print("下面全是假数据（test-stu-*），用来对着检查排版和口径。")
    for title, note, text in build_previews():
        print(f"\n--- {title} ---")
        print(f"（{note}）")
        print(text)


async def _post_text(webhook: str, text: str) -> tuple[bool, dict]:
    """往群机器人 webhook 发一条文本，返回 ``(是否成功, 原始回包)``。"""
    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        _miss("没有安装 httpx，无法发送（pip install httpx）")
        return False, {}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook, json={"msgtype": "text", "text": {"content": text}})
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {}
    return int(data.get("errcode") or 0) == 0, data


async def send_sample(group_id: str) -> bool:
    """往群里发该群**本该收到**的真实样例。

    班级群只发「课后提示 + 班级周报概览」，绝不发单个学生的分数 ——
    测试也不能破例，否则样例本身就是一次数据外发。
    """
    target = next((g for g in groups.load_groups() if g.id == group_id), None)
    if target is None:
        _miss(f"找不到群 {group_id}（先看第 1 项的群配置）")
        return False

    items = build_previews()
    picks = [items[1], items[3]] if target.is_class else [items[0], items[2]]
    all_ok = True
    for title, _note, text in picks:
        ok, data = await _post_text(target.webhook, text)
        if ok:
            _ok(f"{title} 已发到 {target.name}")
        else:
            all_ok = False
            _miss(
                f"{title} 发送失败：errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}（93000 = webhook key 不对）"
            )
        await asyncio.sleep(0.5)  # 单机器人 20 条/分钟，别连着怼
    return all_ok


async def send_test(group_id: str) -> bool:
    target = next((g for g in groups.load_groups() if g.id == group_id), None)
    if target is None:
        _miss(f"找不到群 {group_id}（用 --list 看可用 id）")
        return False
    text = "【学情助手】连通性测试：这条消息来自 AI 教师课后学情推送。收到即可，无需回复。"
    ok, data = await _post_text(target.webhook, text)
    if ok:
        _ok(f"测试消息已发到 {target.id} / {target.name}，去群里看一眼")
        return True
    _miss(f"发送失败：errcode={data.get('errcode')} errmsg={data.get('errmsg')}（93000 = webhook key 不对）")
    return False


async def main_async(args: argparse.Namespace) -> int:
    print(f"群配置：{groups.GROUPS_PATH}")
    has_groups = check_groups()
    check_config()
    check_data()
    tcp_ok = True if args.no_tcp else check_tcp()

    if args.preview:
        preview()
        return 0 if has_groups else 1

    if args.sample:
        print("\n=== 5. 发送学情样例 ===")
        if not has_groups:
            _miss("群配置不可用，先修第 1 项")
            return 1
        if not tcp_ok:
            return 1
        return 0 if await send_sample(args.sample) else 1

    if args.send:
        print("\n=== 5. 发送测试消息 ===")
        if not has_groups:
            _miss("群配置不可用，先修第 1 项")
            return 1
        if not tcp_ok:
            return 1
        return 0 if await send_test(args.send) else 1

    print("\n提示：--preview 离线看文案；--send <群id> 发连通性测试；"
          "--sample <群id> 发真实样例；--no-tcp 跳过网络检查。")
    return 0 if has_groups else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="微信推送接入自检")
    parser.add_argument("--send", default="", help="往指定群 id 发一条测试消息（会真的发）")
    parser.add_argument("--sample", default="", help="往指定群 id 发学情样例（会真的发，按群性质选口径）")
    parser.add_argument("--preview", action="store_true", help="离线打印四种文案样例，不发消息")
    parser.add_argument("--no-tcp", action="store_true", help="跳过网络连通检查")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
