"""家长服务号微信身份绑定；所有写操作限定当前登录家长。"""
from __future__ import annotations

import html
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from core.auth.deps import require_role
from core.wechat import identity, official

router = APIRouter()
COOKIE = "wechat_bind_browser"
NO_STORE = {"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"}


def account_user(response: Response, user: dict = Depends(require_role("user", "admin"))) -> dict:
    response.headers.update(NO_STORE)
    if not settings.auth_enable:
        raise HTTPException(status_code=403, detail="账号体系未启用")
    return user


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    consent: bool = False


class PreferenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _call(func, *args) -> dict:
    try:
        return func(*args)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.get("/api/wechat/me")
def my_wechat(user: dict = Depends(account_user)) -> dict:
    return _call(identity.status, user["id"])


@router.get("/api/wechat/me/course-updates")
def my_course_updates(user: dict = Depends(account_user)) -> dict:
    from core.wechat.user_notices import list_notices
    rows = list_notices(user["id"])
    return {"ok": True, "notices": [{k: row[k] for k in ("id", "title", "content", "send_at", "status")}
                                    for row in rows if row["status"] != "cancelled"]}


@router.post("/api/wechat/me/bind")
def start_binding(user: dict = Depends(account_user)) -> dict:
    return _call(identity.create_session, user["id"])


@router.get("/api/wechat/me/bind/{session_id}")
def binding_status(session_id: str, user: dict = Depends(account_user)) -> dict:
    return _call(identity.session_status, user["id"], session_id)


@router.post("/api/wechat/me/confirm")
def confirm_binding(body: ConfirmBody, user: dict = Depends(account_user)) -> dict:
    if not body.consent:
        raise HTTPException(status_code=400, detail="请确认是本人微信并同意接收本账号的学习情况和课程更新通知")
    return _call(identity.confirm_binding, user["id"], body.session_id)


@router.delete("/api/wechat/me/bind")
def cancel_binding(user: dict = Depends(account_user)) -> dict:
    return _call(identity.cancel_session, user["id"])


@router.patch("/api/wechat/me")
def change_preference(body: PreferenceBody, user: dict = Depends(account_user)) -> dict:
    return _call(identity.set_enabled, user["id"], body.enabled)


@router.delete("/api/wechat/me")
def remove_binding(user: dict = Depends(account_user)) -> dict:
    return _call(identity.unbind, user["id"])


def _page(message: str, status_code: int = 200) -> HTMLResponse:
    content = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width,initial-scale=1">'
               '<title>微信绑定</title></head><body><main><h1>微信绑定</h1><p>'
               + html.escape(message) + '</p><p>请返回发起绑定的用户配置页继续操作。'
               '如果不是您本人发起，请关闭此页面，不要确认绑定。</p></main></body></html>')
    headers = {**NO_STORE, "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"}
    return HTMLResponse(content, status_code=status_code, headers=headers)


@router.get("/api/wechat/official/authorize", response_model=None)
def official_authorize(ticket: Annotated[str, Query(min_length=1, max_length=128)]) -> RedirectResponse | HTMLResponse:
    try:
        url, browser = identity.start_authorization(ticket)
    except ValueError as err:
        return _page(str(err), 400)
    demo = official.bind_demo()
    response = RedirectResponse(url, status_code=302, headers=NO_STORE)
    # 演示模式走本机 HTTP，Secure Cookie 会被浏览器丢弃，导致回调校验失败。
    response.set_cookie(COOKIE, browser, max_age=900, secure=not demo, httponly=True,
                        samesite="lax", path="/api/wechat/official")
    return response


@router.get("/api/wechat/official/demo", response_model=None)
def official_demo(state: Annotated[str, Query(min_length=1, max_length=128)]) -> RedirectResponse | HTMLResponse:
    """本地演示授权页：代替微信授权跳转，直接回到本站回调完成模拟授权。"""
    if not official.bind_demo():
        return _page("当前未启用本地演示绑定，请使用微信扫码完成授权。", 404)
    target = official.bind_base_url() + official.CALLBACK_PATH + "?" + urlencode({"state": state, "code": "demo"})
    return RedirectResponse(target, status_code=302, headers=NO_STORE)


@router.get("/api/wechat/official/callback")
def official_callback(
    request: Request,
    state: Annotated[str, Query(max_length=128)] = "",
    code: Annotated[str, Query(max_length=512)] = "",
) -> HTMLResponse:
    demo = official.bind_demo()
    try:
        identity.complete_authorization(state, code, request.cookies.get(COOKIE, ""))
        response = _page("微信身份已授权。请返回原用户配置页，确认是本人扫码后完成绑定；尚未保存绑定关系。")
    except ValueError as err:
        response = _page(str(err), 400)
    response.delete_cookie(COOKIE, path="/api/wechat/official", secure=not demo, httponly=True, samesite="lax")
    return response
