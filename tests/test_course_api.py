"""任务4 专项测试：会话事件分发 + REST 接口 + 入口路由。

覆盖：
* ``core/loop.py`` 新增事件分发（course_start / course_phase / quiz_request / ping）；
* ``server/routes.py`` 新增接口（/api/subjects、/api/course/policy）；
* ``server/app.py`` 入口路由（/ 指向学科选择页，辅导页仍可从 /app 直达）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agents.teacher.agent import TeacherAgent
from config.subjects import SUBJECTS
from core.loop import SessionLoop
from core.state.session import SessionState
from core.supervisor import Supervisor
from models.llm import MockLLM
from models.tts import MockTTS
from server.app import app


def _make_loop() -> tuple[SessionLoop, SessionState, list[dict]]:
    state = SessionState(session_id="loop-1", student_id="stu-1")
    supervisor = Supervisor(state, teacher=TeacherAgent(llm=MockLLM()))
    supervisor.tts = MockTTS()          # 避免真实网络 TTS
    events: list[dict] = []
    state.emit = events.append
    return SessionLoop(state, supervisor), state, events


# ---------------------------------------------------------------- 事件分发
async def test_loop_dispatches_course_events() -> None:
    loop, state, events = _make_loop()

    # 1) course_start -> 记录学科、开启辅导课、教师打招呼
    await loop._dispatch({"type": "course_start", "subject": "数学"})  # noqa: SLF001
    assert state.course_active is True
    assert state.subject == "数学"
    assert state.course_deadline is not None
    assert any(e["type"] == "course_state" for e in events)

    # 2) quiz_request -> 生成模拟题并进入待作答
    await loop._dispatch({"type": "quiz_request", "topic": "一元一次方程"})  # noqa: SLF001
    assert state.quiz_count == 1
    assert state.pending_question is not None
    assert any(e["type"] == "ai_quiz" for e in events)

    # 3) course_phase -> 更新阶段
    await loop._dispatch({"type": "course_phase", "phase": "lecture"})  # noqa: SLF001
    assert state.course_phase == "lecture"

    # 4) course_phase=ended -> 结束辅导
    await loop._dispatch({"type": "course_phase", "phase": "ended"})  # noqa: SLF001
    assert state.course_active is False
    assert state.course_phase == "ended"

    # 5) ping 仍然可用（回归保护）
    await loop._dispatch({"type": "ping", "ts": 123})  # noqa: SLF001
    assert any(e["type"] == "pong" for e in events)


async def test_loop_ignores_unknown_and_bad_events() -> None:
    loop, state, _events = _make_loop()
    await loop._dispatch({"type": "totally-unknown"})  # noqa: SLF001  不应抛异常
    await loop._dispatch({"type": "course_phase", "phase": "bogus"})  # noqa: SLF001
    assert state.course_phase == "opening"      # 非法阶段不被写入
    await loop._dispatch({"type": "course_start", "subject": ""})  # noqa: SLF001
    assert state.course_active is True


# ---------------------------------------------------------------- REST 接口
def test_subjects_endpoint_returns_metadata() -> None:
    client = TestClient(app)
    resp = client.get("/api/subjects")
    assert resp.status_code == 200
    subjects = resp.json()["subjects"]
    assert len(subjects) == len(SUBJECTS)
    first = subjects[0]
    assert {"key", "name", "emoji", "desc", "grades", "color"} <= set(first)


def test_course_policy_endpoint_returns_phases() -> None:
    client = TestClient(app)
    resp = client.get("/api/course/policy")
    assert resp.status_code == 200
    data = resp.json()

    assert data["enable"] is True
    assert data["duration_seconds"] == data["duration_minutes"] * 60
    assert data["quiz_enable"] is True
    assert [p["phase"] for p in data["phases"]] == [
        "opening",
        "lecture",
        "interaction",
        "summary",
    ]
    assert data["phases"][0]["start_seconds"] == 0
    assert data["phases"][-1]["end_seconds"] == data["duration_seconds"]


# ---------------------------------------------------------------- 入口路由
def test_root_serves_subject_select_page() -> None:
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "选择学科" in resp.text            # 入口页是学科选择页
    assert "/api/subjects" in resp.text        # 且从后端拉取学科清单
    assert "no-store" in resp.headers.get("cache-control", "")


def test_static_pages_are_mounted() -> None:
    client = TestClient(app)
    tutor = client.get("/app/student_tablet/index.html")
    assert tutor.status_code == 200
    assert "course-bar" in tutor.text

    select = client.get("/app/subject_select/index.html")
    assert select.status_code == 200
    assert "选择学科" in select.text

    teacher_web = client.get("/app/teacher_web/index.html")
    assert teacher_web.status_code == 200
