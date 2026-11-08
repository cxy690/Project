"""课程通知机器人管理接口，仅 admin 可访问。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from core.auth.deps import require_developer
from core.wechat import bindings, course_robot


def _admin(response: Response, user: dict = Depends(require_developer)) -> dict:
    response.headers["Cache-Control"] = "no-store, private"
    return user


class _SafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as err:
                detail = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in err.errors()]
                raise HTTPException(status_code=422, detail=detail, headers={"Cache-Control": "no-store"}) from err

        return safe_handler


router = APIRouter(prefix="/api/admin/wechat/course-robot", dependencies=[Depends(_admin)], route_class=_SafeRoute)


def _call(func, *args):
    try:
        return func(*args)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.get("/robots")
def robots() -> dict:
    return {"ok": True, **course_robot.list_robots()}


@router.post("/robots")
def create_robot(body: course_robot.RobotInput) -> dict:
    return {"ok": True, "robot": _call(course_robot.save_robot, body)}


@router.put("/robots/{robot_id}")
def update_robot(robot_id: str, body: course_robot.RobotInput) -> dict:
    return {"ok": True, "robot": _call(course_robot.save_robot, body, robot_id)}


@router.get("/students")
def students() -> dict:
    return {"ok": True, "students": bindings.list_options()["students"]}


@router.get("/courses")
def courses(robot_id: str) -> dict:
    return {"ok": True, "courses": _call(course_robot.list_courses, robot_id)}


@router.post("/courses")
def create_course(body: course_robot.CourseInput) -> dict:
    return {"ok": True, "course": _call(course_robot.save_course, body)}


@router.put("/courses/{course_id}")
def update_course(course_id: str, body: course_robot.CourseInput) -> dict:
    return {"ok": True, "course": _call(course_robot.save_course, body, course_id)}


@router.delete("/courses/{course_id}")
def cancel_course(course_id: str) -> dict:
    _call(course_robot.cancel_course, course_id)
    return {"ok": True}


@router.get("/preview")
def preview(robot_id: str) -> dict:
    return {"ok": True, **_call(course_robot.preview, robot_id)}


@router.get("/notices")
def notices(robot_id: str) -> dict:
    return {"ok": True, "notices": _call(course_robot.list_notices, robot_id)}
