"""管理员微信助手接口：绑定、预览、发送、发送记录。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from core.auth.deps import require_role

require_developer = require_role("admin")
from core.wechat import bindings, service
from server.wechat_identity_routes import router as identity_router
from server.wechat_course_routes import router as course_router

router = APIRouter()
router.include_router(identity_router)
router.include_router(course_router)


class BindingBody(BaseModel):
    guardian_id: str
    student_id: str
    channel: str = "mock"
    target: str = ""
    enabled: bool = True


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = ""
    guardian_id: str = ""
    student_id: str = ""
    date: str = ""
    force: bool = False

    def recipients(self) -> tuple[str, str]:
        if self.user_id:
            if any(value and value != self.user_id for value in (self.guardian_id, self.student_id)):
                raise ValueError("user_id 与兼容字段冲突")
            return self.user_id, self.user_id
        if not self.guardian_id or not self.student_id:
            raise ValueError("请选择接收用户")
        return self.guardian_id, self.student_id


class CourseNoticeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=3000)
    send_at: datetime


class TestMessageBody(BaseModel):
    guardian_id: str
    confirmed: bool = False


def _guard(user: dict) -> None:
    """require_developer 已做门禁；此函数只为路由可读性保留。"""
    if not user:
        raise HTTPException(status_code=401, detail="需要管理员登录")


@router.get("/api/admin/wechat/status")
def wechat_status(
    date: str = "", user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    return {"ok": True, **service.status_summary(date)}


@router.post("/api/admin/wechat/test")
def wechat_test(
    body: TestMessageBody, user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="测试会发送通知，需要显式确认")
    try:
        return service.send_test_notification(body.guardian_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.get("/api/admin/wechat/options")
def wechat_options(user: dict = Depends(require_developer)) -> dict:
    _guard(user)
    return {"ok": True, **bindings.list_options()}


@router.get("/api/admin/wechat/bindings")
def wechat_bindings(user: dict = Depends(require_developer)) -> dict:
    _guard(user)
    rows = bindings.list_bindings()
    return {"ok": True, "bindings": rows, "count": len(rows)}


@router.post("/api/admin/wechat/bindings")
def wechat_add_binding(
    body: BindingBody, user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    try:
        return bindings.add_binding(
            guardian_id=body.guardian_id,
            student_id=body.student_id,
            channel=body.channel,
            target=body.target,
            enabled=body.enabled,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.delete("/api/admin/wechat/bindings")
def wechat_delete_binding(
    guardian_id: str, student_id: str, user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    ok = bindings.delete_binding(guardian_id, student_id)
    if not ok:
        raise HTTPException(status_code=404, detail="绑定不存在")
    return {"ok": True}


@router.get("/api/admin/wechat/preview")
def wechat_preview(
    student_id: str, date: str = "", user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    try:
        return service.preview_daily_report(student_id, date)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.post("/api/admin/wechat/send")
def wechat_send(
    body: SendBody, user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    try:
        recipient, owner = body.recipients()
        return service.send_daily_report(recipient, owner, body.date, force=body.force)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.post("/api/admin/wechat/run")
def wechat_run(
    date: str = "", force: bool = False, user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    results = service.send_all_today(date, force=force)
    return {
        "ok": True,
        "results": results,
        "sent": sum(1 for item in results if item.get("status") == "sent" and not item.get("skipped")),
        "real_sent": sum(1 for item in results if item.get("status") == "sent"
                         and not item.get("simulated") and not item.get("skipped")),
        "simulated": sum(1 for item in results if item.get("status") == "sent"
                         and item.get("simulated") and not item.get("skipped")),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
    }


@router.get("/api/admin/wechat/deliveries")
def wechat_deliveries(
    date: str = "", limit: int = 100, user: dict = Depends(require_developer)
) -> dict:
    _guard(user)
    rows = service.list_deliveries(date, limit=limit)
    return {
        "ok": True,
        "date": date or service.today(),
        "deliveries": rows,
        "count": len(rows),
        "provider": settings.wechat_provider,
        "report_time": settings.wechat_report_time,
    }


@router.get("/api/admin/wechat/course-updates")
def course_updates(user_id: str = "", user: dict = Depends(require_developer)) -> dict:
    from core.wechat import user_notices
    return {"ok": True, "notices": user_notices.list_notices(user_id)}


@router.post("/api/admin/wechat/course-updates/preview")
def preview_course_update(body: CourseNoticeBody, user: dict = Depends(require_developer)) -> dict:
    from core.wechat import user_notices
    try:
        return user_notices.preview(body.user_id, body.title, body.content)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.post("/api/admin/wechat/course-updates")
def create_course_update(body: CourseNoticeBody, user: dict = Depends(require_developer)) -> dict:
    from core.wechat import user_notices
    try:
        return {"ok": True, "notice": user_notices.save(body.user_id, body.title, body.content, body.send_at)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.put("/api/admin/wechat/course-updates/{notice_id}")
def edit_course_update(notice_id: str, body: CourseNoticeBody, user: dict = Depends(require_developer)) -> dict:
    from core.wechat import user_notices
    try:
        return {"ok": True, "notice": user_notices.save(body.user_id, body.title, body.content, body.send_at, notice_id)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.delete("/api/admin/wechat/course-updates/{notice_id}")
def cancel_course_update(notice_id: str, user: dict = Depends(require_developer)) -> dict:
    from core.wechat import user_notices
    try:
        user_notices.cancel(notice_id)
        return {"ok": True}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
