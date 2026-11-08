# -*- coding: utf-8 -*-
"""临时：把平台管理员 admin 的密码重置为 123456。"""
from __future__ import annotations

from sqlalchemy import select

from core.auth.security import hash_password
from db.base import get_session
from db.models import User

with get_session() as db:
    user = db.scalar(select(User).where(User.username == "admin"))
    if user is None:
        print("admin 不存在，未做修改")
    else:
        user.password_hash = hash_password("123456")
        db.commit()
        print(f"admin 密码已重置：role={user.role} org={user.org_id!r} name={user.display_name}")
