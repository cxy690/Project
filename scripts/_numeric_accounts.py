# -*- coding: utf-8 -*-
"""临时脚本（用完即删）：把演示账号改成纯数字账号 + 数字密码，并补两个机构老师账号。"""
from __future__ import annotations

from sqlalchemy import select

from core.auth.security import hash_password
from core.auth.service import AuthError, register_user
from db.base import get_session
from db.models import User

# 旧账号 -> (新账号, 新密码)：直接改名，保留 id（学情记录挂在 id 上，不能删）
RENAMES = (
    ("schoola_student", "1001", "100100"),
    ("schoolb_student", "1002", "100200"),
    ("schoola_admin", "9001", "900100"),
    ("schoolb_admin", "9002", "900200"),
)

NEW_TEACHERS = (
    ("2001", "200100", "schoola", "A机构老师"),
    ("2002", "200200", "schoolb", "B机构老师"),
)


def main() -> None:
    with get_session() as db:
        for old, new, password in RENAMES:
            user = db.scalar(select(User).where(User.username == old))
            if user is None:
                print(f"skip   {old} 不存在")
                continue
            if db.scalar(select(User).where(User.username == new)) is not None:
                print(f"skip   {new} 已被占用")
                continue
            user.username = new
            user.password_hash = hash_password(password)
            print(f"rename {old} -> {new}（id 不变）")
        db.commit()

    for username, password, org, display in NEW_TEACHERS:
        try:
            register_user(username, password, role="teacher",
                          display_name=display, org_id=org)
            print(f"create {username}（{org} 老师）")
        except AuthError as err:
            print(f"skip   {username} -> {err}")

    with get_session() as db:
        print("-" * 60)
        for user in db.scalars(select(User).order_by(User.org_id, User.username)).all():
            if user.username.isdigit():
                print(f"{user.username}  {user.role:8s} org={user.org_id}  {user.display_name}")


if __name__ == "__main__":
    main()
