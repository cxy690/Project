"""pytest 兜底配置。

三件事：
1. **测试隔离**：清空外部服务密钥，保证单测离线可跑、不产生费用、不因网络抖动而挂住。
   必须在导入 ``config.settings`` / ``models.*`` 之前设置——环境变量优先级高于 .env 文件，
   密钥为空时 get_llm()/get_asr()/get_tts() 一律降级为 Mock。
   历史坑：未做隔离时，测试会真的去调 .env 里配的 LLM（deepseek-chat），套件表现为「卡住」。
2. **运行时目录隔离**：把观测 / 学习层的落盘目录（裁决 / 经验 / 技能战绩 / 学情记忆 /
   token 用量 / 白板快照 / 日志）重定向到临时目录。
   历史坑（2026-10-14）：没有这层隔离时，测试里跑完整回合会把**测试数据写进仓库真实
   ``data/`` 目录**——一次 pytest 就往 ``data/teacher_experience/math.jsonl`` 追加了 38 行
   回写、往 ``data/raw/verdicts`` 追加了 88 行裁决，开发机上的「技能自我更新看板」因此
   凭空冒出「课堂效果确认」记录（用户并没有上课）。同样必须在导入 settings 之前用环境变量改。
3. **异步兜底**：未安装 pytest-asyncio 时提供极简 async 执行器，保证 async 测试也能运行。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from pathlib import Path

# --- 1. 测试隔离（必须最先执行）---
# INTENT_LLM_API_KEY 也要清：意图分类小模型是独立 key，不清的话单测会真的调外部 API
# （慢、依赖网络、且产生费用）。
for _key in (
    "LLM_API_KEY", "INTENT_LLM_API_KEY", "ASR_API_KEY", "TTS_API_KEY", "OCR_CLOUD_KEY", "VLM_API_KEY"
):
    os.environ[_key] = ""
# 显式走 Mock，避免依赖「密钥为空」这一隐式降级
os.environ["LLM_PROVIDER"] = "mock"
os.environ["ASR_PROVIDER"] = "mock"
os.environ["TTS_PROVIDER"] = "mock"
# 数字人：测试不需要真渲染引擎（否则会去连 :8012 的 MuseTalk 服务）
os.environ["AVATAR_BACKEND"] = "mock"
os.environ["AVATAR_ENABLE"] = "false"
# 真题库（ai_teacher.exercise_item）：单测不连真库（否则每次出题都会试连本机 MySQL）。
# 需要题库路径的用例自行 monkeypatch settings.exercise_bank_enable=True
# 并 exercise_bank.set_picker(fake) 注入假题源。
os.environ["EXERCISE_BANK_ENABLE"] = "false"
# 编排与答疑形态固定为「自研图 + 结构化答疑」：否则开发者 .env 里的
# ORCHESTRATOR=langgraph / BOARD_FIRST_MODE=single_call 会把测试带到另一条链路上，
# 结果「在 A 机器通过、在 B 机器失败」。要测另一条链路的用例自行 monkeypatch settings。
os.environ["ORCHESTRATOR"] = "builtin"
os.environ["BOARD_FIRST_MODE"] = "off"

# --- 2. 运行时产物目录隔离（同样必须最先执行，见文件头「历史坑」）---
# 这些目录都是 append-only 的运行时产物：测试跑完整回合（答疑 / 复盘 / 判题）会往里写，
# 不隔离就会污染开发机上真实使用的数据与看板。统一指向一个临时目录，便于随时翻查，
# 目录本身留在系统 TEMP 下（前缀 edu_test_runtime_），由系统或人工清理。
_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="edu_test_runtime_"))
for _dir_key, _sub in (
    ("VERDICT_DIR", "verdicts"),                      # 回合裁决（core/verdict.py）
    ("SKILL_EXPERIENCE_DIR", "teacher_experience"),   # 教师经验（core/skills/experience）
    ("AGENT_SKILL_USAGE_DIR", "skill_usage"),         # Agent 技能战绩
    ("MEMORY_DIR", "memory"),                         # 学情记忆（未成年人数据，尤其不能外泄）
    ("LLM_USAGE_DIR", "llm_usage"),                   # token 用量（core/llm_usage.py）
    ("BOARD_SNAPSHOT_DIR", "board_snapshots"),        # 白板快照
    ("LOG_DIR", "logs"),                              # 运行日志（别混进服务日志）
):
    os.environ[_dir_key] = str(_RUNTIME_ROOT / _sub)
os.environ["SAFETY_AUDIT_PATH"] = str(_RUNTIME_ROOT / "safety_events.jsonl")

import pytest  # noqa: E402

try:  # 已安装 pytest-asyncio 时交给它处理
    import pytest_asyncio  # noqa: F401

    _HAS_ASYNCIO = True
except ImportError:  # pragma: no cover - 取决于环境
    _HAS_ASYNCIO = False


if not _HAS_ASYNCIO:

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(pyfuncitem: pytest.Function):  # type: ignore[no-redef]
        testfunc = pyfuncitem.obj
        if inspect.iscoroutinefunction(testfunc):
            kwargs = {
                name: pyfuncitem.funcargs[name]
                for name in pyfuncitem._fixtureinfo.argnames
            }
            asyncio.run(testfunc(**kwargs))
            return True
        return None
