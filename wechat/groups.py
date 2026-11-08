"""群注册表：哪些群能收到哪些学情（学生 ↔ 群 的多对多映射）。

为什么单独一层：课后推送不是「发到一个全局 webhook」，而是「这个学生所在的群」。
而**群的性质决定了能发什么**：

* ``parent``（一对一家校群，群里只有这位家长和老师）：可以发该生本节明细；
* ``class``（班级群，多个家长）：**只发「已生成，请私聊查看」或班级聚合概览**。
  任何单个学生的分数、掌握度、待改进项都不能出现在这里 —— 群里其他家长能看到，
  是未成年人数据外发。默认值就是 ``class``（配置写错也只会更保守）。

配置放在 ``data/wechat/groups.json``（与 ``bindings.json`` 同目录）：
该目录已 gitignore，``scripts/sync_cloud.py`` 也把它排除在同步之外 ——
不同环境对应不同家长群，每台机器各配一份，别指望同步带过去。

样例见 ``wechat/groups.example.json``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.logging import get_logger

log = get_logger("wechat.groups")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GROUPS_PATH = PROJECT_ROOT / "data" / "wechat" / "groups.json"

#: 一对一家校群：可发该生明细
KIND_PARENT = "parent"
#: 班级群（多个家长）：只发提示 / 班级聚合
KIND_CLASS = "class"
#: 内部群（教师 / 运营自用，群里没有家长）：可发多学生明细
KIND_STAFF = "staff"

_ALLOWED_KINDS = {KIND_PARENT, KIND_CLASS, KIND_STAFF}


@dataclass(frozen=True)
class Group:
    """一个可推送的微信群（对应一个群机器人 webhook）。"""

    id: str
    name: str = ""
    webhook: str = ""
    kind: str = KIND_CLASS
    student_ids: tuple[str, ...] = ()
    lesson_end: bool = True       # 课后是否推
    weekly: bool = True           # 周报是否推
    daily: bool = True            # 每天定点推「当日学情」
    show_student_name: bool = True  # 班级群课后提示里是否写出学生名

    @property
    def is_class(self) -> bool:
        """是否班级群（**未知 kind 一律按班级群处理**，宁可少发不可多发）。

        ``load_groups`` 会把无法识别的 kind 归一到 ``class``，所以这里只认严格等于。
        """
        return self.kind == KIND_CLASS

    @property
    def allows_detail(self) -> bool:
        """是否允许出现单个学生的分数（内部群 / 一对一家校群）。"""
        return self.kind in (KIND_PARENT, KIND_STAFF)


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _ids(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return ()
    out = [str(item).strip() for item in raw if str(item or "").strip()]
    return tuple(dict.fromkeys(out))  # 去重且保持顺序


def load_groups() -> list[Group]:
    """读取群配置；文件不存在 / 写坏一律返回空列表（推送随之关闭，不影响主链路）。"""
    try:
        raw = json.loads(GROUPS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as err:  # noqa: BLE001 - 配置坏了只是不发，不能让服务起不来
        log.warning("群配置读取失败（按未配置处理）：{}", err)
        return []

    items: Any = raw.get("groups") if isinstance(raw, dict) else raw
    if isinstance(items, dict):  # 兼容 {"g1": {...}} 的写法
        items = [{"id": key, **value} for key, value in items.items() if isinstance(value, dict)]
    if not isinstance(items, list):
        log.warning("群配置格式不对（应为 groups 数组），已忽略")
        return []

    groups: list[Group] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        gid = str(item.get("id") or "").strip()
        webhook = str(item.get("webhook") or "").strip()
        if not gid or not webhook.startswith("http"):
            log.warning("群配置第 {} 条缺少 id / webhook，已跳过", index + 1)
            continue
        kind = str(item.get("kind") or KIND_CLASS).strip().lower()
        if kind not in _ALLOWED_KINDS:
            log.warning("群 %s 的 kind=%s 无法识别，按班级群处理（保守）", gid, kind)
            kind = KIND_CLASS
        groups.append(
            Group(
                id=gid,
                name=str(item.get("name") or gid).strip(),
                webhook=webhook,
                kind=kind,
                student_ids=_ids(item.get("student_ids")),
                lesson_end=_truthy(item.get("lesson_end"), True),
                weekly=_truthy(item.get("weekly"), True),
                daily=_truthy(item.get("daily"), True),
                show_student_name=_truthy(item.get("show_student_name"), True),
            )
        )
    return groups


def groups_for_student(
    student_id: str,
    *,
    for_lesson: bool = False,
    for_weekly: bool = False,
    kind: str = "",
) -> list[Group]:
    """该学生所在、且允许接收指定类型消息的群。"""
    sid = str(student_id or "").strip()
    if not sid:
        return []
    out: list[Group] = []
    for group in load_groups():
        if sid not in group.student_ids:
            continue
        if for_lesson and not group.lesson_end:
            continue
        if for_weekly and not group.weekly:
            continue
        if kind and group.kind != kind:
            continue
        out.append(group)
    return out


def student_label(student_id: str, group: Group | None = None) -> str:
    """班级群提示里的称呼：优先 ``users.display_name``，查不到回退脱敏 id。

    ``group.show_student_name=False`` 时返回空串（由文案层写成「有同学」）。
    查库失败**绝不能**影响推送 —— 学情是增值功能，名字降级成「学生 1234」也要发。
    """
    if group is not None and not group.show_student_name:
        return ""
    sid = str(student_id or "").strip()
    if not sid:
        return ""
    try:
        from db.base import get_session  # noqa: PLC0415 - 通知链路里按需连库
        from db.models import User  # noqa: PLC0415

        with get_session() as db:
            row = db.get(User, sid)
            if row is not None:
                name = str(getattr(row, "display_name", "") or getattr(row, "username", "") or "")
                if name.strip():
                    return name.strip()
    except Exception as err:  # noqa: BLE001 - 查不到就用脱敏 id
        log.debug("学生姓名查询失败（{}），使用脱敏 id", err)
    return f"学生{sid[-4:]}"


__all__ = [
    "GROUPS_PATH",
    "KIND_CLASS",
    "KIND_PARENT",
    "KIND_STAFF",
    "Group",
    "groups_for_student",
    "load_groups",
    "student_label",
]
