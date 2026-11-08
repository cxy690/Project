# -*- coding: utf-8 -*-
"""临时脚本（用完即删）：把六个演示账号改成「机构字母 + 编号」，密码统一 123456。

只改 username / password_hash，主键 id 不动 —— 学情、课程、已签发令牌（sub=id）都不受影响。
"""
from __future__ import annotations

from sqlalchemy import select

from core.auth.security import hash_password
from db.base import get_session
from db.models import User

NEW_PASSWORD = "123456"

# (旧账号, 新账号, 说明)
RENAMES = (
    ("1001", "a1002", "A机构 学生"),
    ("1002", "b1002", "B机构 学生"),
    ("2001", "a2002", "A机构 老师"),
    ("2002", "b2002", "B机构 老师"),
    ("9001", "a9002", "A机构 管理员"),
    ("9002", "b9002", "B机构 管理员"),
)


def main() -> None:
    with get_session() as db:
        for _, new, _ in RENAMES:
            if db.scalar(select(User).where(User.username == new)) is not None:
                print(f"abort：目标账号 {new} 已被占用，未做任何修改")
                return

        for old, new, label in RENAMES:
            user = db.scalar(select(User).where(User.username == old))
            if user is None:
                print(f"skip  {old} 不存在")
                continue
            user.username = new
            user.password_hash = hash_password(NEW_PASSWORD)
            print(f"rename {old} -> {new}  {label}  org={user.org_id}")
        db.commit()

    with get_session() as db:
        print("-" * 62)
        rows = db.scalars(select(User).order_by(User.org_id, User.username)).all()
        for user in rows:
            print(f"{user.username:<8} {user.role:<8} org={user.org_id:<8} {user.display_name}")


if __name__ == "__main__":
    main()
