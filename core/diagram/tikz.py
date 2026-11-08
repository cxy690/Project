"""TikZ 教学图：题目 → 分步 LaTeX 源码 → 编译成 PNG 帧，给数字人老师边讲边画用。

与 :mod:`core.diagram.spec`（前端 SVG 引擎）的分工
------------------------------------------------
====  ======================================  ==========================================
引擎  怎么出图                                 什么时候用
====  ======================================  ==========================================
svg   模型产 JSON，浏览器现场画 SVG            秒级、逐笔描边动画、可任意跳步
tikz  模型产 TikZ 源码，xelatex 编译成 PNG 帧  LaTeX 级排版：分式、根式、矩阵、精确刻度
====  ======================================  ==========================================

为什么不让模型写整份 ``.tex``
--------------------------
导言区由本模块拼装（宏包白名单 + 固定 16:9 画布）。模型只写 ``tikzpicture`` 主体，好处有三：
1. **逐帧不跳**：画布恒定 16cm×9cm，几步讲完不会因为范围变化而整体缩放；
2. **编译可控**：模型塞不进 ``\\usepackage``/``\\input``，导言区唯一；
3. **安全**：LaTeX 是图灵完备的，``\\write18`` 能执行 shell 命令。这里三重闸门 ——
   宏包白名单、静态黑名单、``xelatex -no-shell-escape``；再叠加超时与临时目录隔离。
   （所以这条链路是**真的需要**把不可信源码关进笼子，和 SVG 引擎不同。）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

from config.logging import get_logger
from config.settings import settings
from core.diagram import skill as diagram_skill
from models.base import BaseLLMClient
from models.llm import get_llm

log = get_logger("core.diagram.tikz")

#: 画布：16:9（白板右栏的宽高比接近这个，投影时不浪费边）
CANVAS_W, CANVAS_H = 16.0, 9.0

#: 导出位图分辨率。180 dpi 下 16cm×9cm ≈ 1134×638，投影到 1080p 足够锐。
DPI = 180

#: 缓存版本：改了提示词/导言区/裁切策略就加一，旧图自动作废重画
#: （否则学生看到的还是上一版提示词画出来的图，改了提示词却毫无效果）
CACHE_VERSION = 5

#: 导言区兜底：**仅在技能模板缺失时生效**。
#: 正常路径读 ``.agents/agent_skills/tikz-teacher-diagram/templates/slide_16x9_ctex.tex``，
#: 那份才是事实源；这里只保证技能文件被误删时课堂不出空白屏。改规则请改技能文件，
#: 不要改这里（两边不一致时运行期优先用技能文件）。
_PREAMBLE = r"""\documentclass[border=0pt]{standalone}
\usepackage{ctex}
\usepackage{amsmath,amssymb}
\usepackage{tikz}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}
\usetikzlibrary{arrows.meta,positioning,calc,fit,backgrounds,patterns,%
  decorations.pathreplacing,intersections,angles,quotes}
\begin{document}
\begin{tikzpicture}[font=\sffamily,>=Stealth,
  lab/.style={font=\sffamily\normalsize,align=center,text=black!72},
  directlabel/.style={font=\sffamily\normalsize,align=center,text=black!72},
  box/.style={draw,rounded corners=2pt,align=center,inner sep=6pt,
    font=\sffamily\normalsize,text=black!85},
  explainer/.style={draw,rounded corners=2pt,align=center,inner sep=6pt,
    font=\sffamily\normalsize,text width=5.2cm,text=black!85},
  arr/.style={->,line width=1.1pt,black!75}]
  \useasboundingbox (0,0) rectangle (16,9);
@@BODY@@
\end{tikzpicture}
\end{document}
"""

#: 黑名单：这些命令能读文件、写文件、执行命令，或改掉编译期的类别码
_FORBIDDEN = re.compile(
    r"\\(?:write18|input|include|includeonly|openout|openin|read|immediate|special|"
    r"directlua|catcode|csname|usepackage|documentclass|RequirePackage|includegraphics|"
    r"shipout|everyjob|AtBeginDocument|AtEndDocument|newwrite|write|loop|newread|"
    r"endlinechar|escapechar|makeatletter|def|let|luaexec)\b"
)

#: 生成指令兜底：**仅在技能文件缺失/解析失败时生效**。
#: 事实源是 ``.agents/agent_skills/tikz-teacher-diagram/SKILL.md`` 的「出图规划提示词」
#: 小节（运行期按 mtime 重载，改 md 即生效）。改规则请改那份，不要改这里。
#: 用 ``@@QUESTION@@`` 占位而非 ``{}`` —— 指令里全是 LaTeX 花括号，走 replace 更安全。
_PROMPT = """你是中学数学老师，要用 LaTeX/TikZ 画一张「分步讲解」的教学图，配合口播一步步画出来。

只输出一个 JSON，不要解释：
{"title": "不超过 14 字",
 "steps": [{"caption": "不超过 20 字", "code": "tikzpicture 主体"}]}

硬约束（违反会导致编译失败，请严格遵守）：
1. 画布固定 16cm × 9cm，坐标范围 0~16（横）、0~9（纵），内容四周留 0.4 的边；
2. 写 3~4 步，**后一步必须包含前一步的全部内容**，只在本步新增元素（像老师在黑板上逐步补画）；
3. 每步的 code 只写 tikzpicture 的主体（不要写 \\begin{tikzpicture}/\\end{tikzpicture}，
   不要写 \\documentclass/\\usepackage），导言区由系统负责；
4. 中文标签 1~4 个字；数学式用 $...$；标签统一用预置样式 lab（已设成 text=black!72）；
   每一步的 code 控制在 800 字符以内（输出太长会被截断成非法 JSON）；
5. 禁用一切外部依赖：不许 \\input、\\includegraphics、\\write18、\\def、\\newcommand；
6. **函数图像一律用 pgfplots 的 axis 环境**（刻度、缩放、交点由它算，不要手画坐标轴）；
   几何作图仍用普通 TikZ 线段/圆/角标；不要编造题干里没有的数值；
7. 文字不要压在曲线或坐标轴上：标签放末端空白处，必要时 anchor=west / above=2pt；
8. **几何点必须用命名坐标，垂足用投影语法算**，不要自己估坐标：
   \\coordinate (A) at (1,1); \\coordinate (C) at (5,5); \\coordinate (D) at ($(A)!(C)!(B)$);
   各点之间一律用 (A)--(B) 连接，保证图上的关系真的成立；
9. **题干点名要作的辅助线，最后一步必须画全**：高、中线、角平分线、垂线、延长线、
   交点——题干给了字母就照用。高用虚线画，垂足处补直角符号；
10. 边长标注必须贴在线段中点、朝**图形外侧**，偏移量写死，别凭感觉放；
    严禁把长度标注丢在图形围起来的内部空白处。

题目：
@@QUESTION@@
"""


class TikzDiagramBuilder:
    """题目 → 分步 TikZ → PNG 帧（失败一律返回 ``None``，绝不打断讲课）。"""

    def __init__(
        self,
        llm: BaseLLMClient | None = None,
        *,
        timeout: float | None = None,
        max_tokens: int | None = None,
        cache_dir: str | Path | None = None,
        dpi: int = DPI,
        compile_timeout: float = 45.0,
        max_steps: int = 4,
    ) -> None:
        self._llm = llm
        self.timeout = float(timeout if timeout is not None else settings.diagram_timeout_seconds)
        self.max_tokens = int(
            max_tokens if max_tokens is not None else settings.diagram_tikz_max_tokens
        )
        self.cache_dir = Path(cache_dir or settings.diagram_tikz_cache_dir)
        self.dpi = dpi
        self.compile_timeout = compile_timeout
        self.max_steps = max_steps

    @property
    def llm(self) -> BaseLLMClient:
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ---------------------------------------------------------------- 对外
    async def build(
        self,
        question: str,
        *,
        subject: str = "",
        on_frame: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """产出 ``{"title": str, "frames": [url, ...]}``；``on_frame`` 用于边编译边推。

        ``on_frame(i, url)`` 在每一帧编译完成后调用一次 —— 首帧一好就推给前端，
        学生不用等四张图全编译完才看到老师开始画。
        """
        text = _clean_question(question)
        if not text:
            return None

        cached = await self._load_cache(text)
        if cached is not None:
            for i, url in enumerate(cached["frames"]):
                if on_frame:
                    await on_frame(i, url)
            return cached

        if self._llm is None and not settings.llm_api_key:
            return None

        # 引擎先探：没有 TeX 就立刻放弃，别白花一次 LLM 调用（那要 3~6 秒）
        engine = resolve_engine()
        if engine is None:
            return None

        try:
            raw = await asyncio.wait_for(
                self.llm.chat_json(
                    [{"role": "user", "content": _plan_prompt(text)}],
                    temperature=0.2,
                    max_tokens=self.max_tokens,
                ),
                timeout=self.timeout,
            )
        except Exception as err:  # noqa: BLE001 - 出图失败不能影响讲课
            log.warning("TikZ 源码生成失败（本轮不画图）：{}", err)
            return None

        steps = _sanitize_steps(raw, max_steps=self.max_steps)
        if not steps:
            log.info("TikZ 源码不可用（未通过校验），本轮不画图")
            return None

        outdir = self._outdir(text)

        # 先把每步都编译成 PDF（同一个画布尺寸），再统一按"所有帧的并集内容框"裁切。
        # 为什么不在编译完一帧就推：逐帧各自裁切会让每一步的图形比例跳变（上一帧占满、
        # 下一帧缩成一小块），学生看着像镜头乱推。统一裁切后帧与帧之间只有"新增笔画"在变。
        workdir = Path(tempfile.mkdtemp(prefix="tikz_", dir=str(outdir)))
        try:
            pdfs: list[Path] = []
            for index, (_caption, code) in enumerate(steps):
                pdf = await self._compile(engine, workdir, index, code)
                if pdf is None:
                    log.warning("TikZ 第 {} 帧编译失败，跳过该帧", index + 1)
                    continue
                pdfs.append(pdf)
            if not pdfs:
                return None

            clip = await asyncio.to_thread(_union_rect, pdfs)
            frames: list[str] = []
            for index, pdf in enumerate(pdfs, start=1):
                png = outdir / f"step-{index}.png"
                await asyncio.to_thread(_pdf_to_png, pdf, png, self.dpi, clip)
                if not png.exists():
                    continue
                url = f"{settings.diagram_public_prefix.rstrip('/')}/{outdir.name}/step-{index}.png"
                frames.append(url)
                if on_frame:
                    await on_frame(len(frames) - 1, url)
            if not frames:
                return None
            result = {
                "v": CACHE_VERSION,
                "title": _text(raw.get("title") if isinstance(raw, dict) else "", 28) or "讲解示意图",
                "frames": frames,
                "captions": [caption for caption, _code in steps][: len(frames)],
            }
            await self._save_cache(text, result)
            log.info("TikZ 教学图就绪：{} 帧 | {}", len(frames), result["title"])
            return result
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    # ---------------------------------------------------------------- 编译
    async def _compile(self, engine: str, workdir: Path, index: int, code: str) -> Path | None:
        """编译一帧。临时目录隔离 + 超时 + 禁 shell escape。"""
        tex = workdir / f"figure{index}.tex"
        try:
            # 用占位符替换而不是 % —— 导言区里有大量 LaTeX 注释的 %，格式化成不了
            template = _preamble()
            tex.write_text(template.replace(diagram_skill.BODY_SLOT, code), encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                engine,
                *_engine_args(engine, workdir, tex),
                cwd=str(workdir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.compile_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("TikZ 编译超时（{}s），放弃该帧", self.compile_timeout)
                return None
            pdf = workdir / f"figure{index}.pdf"
            if proc.returncode != 0 or not pdf.exists():
                log_file = workdir / f"figure{index}.log"
                if log_file.exists():
                    tail = await asyncio.to_thread(_tail, log_file, 600)
                    log.warning("TikZ 编译失败：{}", tail)
                return None
            return pdf
        except OSError as err:
            log.warning("TikZ 源码写入失败（本轮不画图）：{}", err)
            return None

    # ---------------------------------------------------------------- 缓存
    def _outdir(self, question: str) -> Path:
        digest = hashlib.sha1(question.encode("utf-8")).hexdigest()[:16]
        outdir = self.cache_dir / digest
        outdir.mkdir(parents=True, exist_ok=True)
        return outdir

    async def _load_cache(self, question: str) -> dict[str, Any] | None:
        meta = self._outdir(question) / "meta.json"
        try:
            if not meta.exists():
                return None
            data = json.loads(await asyncio.to_thread(meta.read_text, encoding="utf-8"))
            frames = data.get("frames") or []
            if not frames or data.get("v") != CACHE_VERSION:
                return None
            # 只认本地真的还在的帧（/media/diagrams/<hash>/step-N.png）
            for url in frames:
                if not (self._outdir(question) / Path(url).name).exists():
                    return None
            return data
        except Exception as err:  # noqa: BLE001 - 缓存坏了等于没缓存
            log.debug("TikZ 缓存读取失败（忽略）：{}", err)
            return None

    async def _save_cache(self, question: str, result: dict[str, Any]) -> None:
        meta = self._outdir(question) / "meta.json"
        try:
            payload = json.dumps(result, ensure_ascii=False)
            await asyncio.to_thread(meta.write_text, payload, "utf-8")
        except Exception as err:  # noqa: BLE001
            log.debug("TikZ 缓存写入失败（忽略）：{}", err)


# ==================================================================== 技能接线
#: 只在首次降级时告警一次，避免每道题刷屏
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message)


def _plan_prompt(question: str) -> str:
    """生成指令：优先取技能 SKILL.md，缺失时才用内置兜底。"""
    template = diagram_skill.plan_prompt()
    if not template:
        _warn_once(
            "prompt",
            f"技能提示词缺失（{diagram_skill.skill_dir()}），使用内置兜底指令",
        )
        template = _PROMPT
    return template.replace(diagram_skill.QUESTION_SLOT, question)


def _preamble() -> str:
    """导言区：优先取技能模板，缺失时才用内置兜底。"""
    template = diagram_skill.preamble()
    if not template:
        _warn_once(
            "template",
            f"技能模板缺失（{diagram_skill.skill_dir()}），使用内置兜底导言区",
        )
        template = _PREAMBLE
    return template


def _which_engine(candidate: str) -> str | None:
    """解析单个候选：命令名走 PATH，带路径的直接判存在。"""
    raw = str(candidate or "").strip()
    if not raw:
        return None
    if raw.startswith("~") or any(sep in raw for sep in ("/", "\\")):
        path = Path(raw).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which(raw)


def resolve_engine(name: str | None = None) -> str | None:
    """把配置里的 TeX 引擎解析成可执行文件路径。

    支持三种写法：PATH 命令名（``xelatex``）、绝对路径
    （``D:\\texlive\\2024\\bin\\windows\\xelatex.exe``）、家目录路径
    （``~/.workbuddy/tools/tectonic.exe`` —— 本机唯一存在的 TeX 就是这个）。

    顺序：``DIAGRAM_TEX_ENGINE`` 环境变量 → 传入值 / 配置值 → 常见兜底位置。
    全找不到返回 ``None``，调用方据此跳过画图（不抛错、不拖慢讲课）。
    """
    candidates = [
        os.environ.get("DIAGRAM_TEX_ENGINE", ""),
        name if name is not None else settings.diagram_tikz_engine,
        "~/.workbuddy/tools/tectonic.exe",
        "tectonic",
        "xelatex",
    ]
    for candidate in candidates:
        found = _which_engine(candidate)
        if found:
            return found
    _warn_once(
        "engine",
        "找不到任何 TeX 引擎（xelatex/tectonic），TikZ 画图不可用；"
        "可设 DIAGRAM_TEX_ENGINE 指向可执行文件绝对路径",
    )
    return None


def _engine_args(engine: str, workdir: Path, tex: Path) -> list[str]:
    """按引擎族给参数。tectonic 的参数与 TeX Live 不同，且本身无 shell-escape 能力。"""
    if "tectonic" in Path(engine).stem.lower():
        return [
            "--outdir",
            str(workdir),
            "--keep-logs",
            "--chatter",
            "minimal",
            str(tex),
        ]
    return [
        "-no-shell-escape",          # 关键：禁止 \write18 执行命令
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={workdir}",
        str(tex),
    ]


# ==================================================================== 校验

def _clean_question(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:1200]


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _sanitize_steps(raw: Any, *, max_steps: int = 4) -> list[tuple[str, str]]:
    """从模型输出里取可编译的 tikzpicture 主体；任何一项不过关就丢掉该步。

    三道关：``_FORBIDDEN``（安全，代码红线）→ 花括号配对（必然失败）→
    技能自带的 ``check_tikz_safety.check_source``（排版隐患：压字、溢出、未定位标签）。
    """
    if not isinstance(raw, dict):
        return []
    checker = diagram_skill.safety_checker()
    steps: list[tuple[str, str]] = []
    for item in (raw.get("steps") or [])[: max_steps * 2]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        code = _strip_wrapper(code)
        if not code or len(code) > 6000:
            continue
        if _FORBIDDEN.search(code):
            log.warning("TikZ 源码含禁用命令，已丢弃该步")
            continue
        if not _balanced(code):
            continue
        if checker is not None:
            issues = checker(code, fragment=True)
            if issues:
                log.info("TikZ 该步未过技能闸门，已丢弃：{}", "；".join(issues))
                continue
        steps.append((_text(item.get("caption"), 24), code))
        if len(steps) >= max_steps:
            break
    return steps


def _strip_wrapper(code: str) -> str:
    """模型偶尔会连着 documentclass/环境一起给，这里剥掉，只留主体。"""
    code = re.sub(r"(?s)\\documentclass.*?\\begin\{document\}", "", code)
    code = code.replace("\\end{document}", "")
    code = re.sub(r"(?s)\\begin\{tikzpicture\}(\[[^\]]*\])?", "", code)
    code = code.replace("\\end{tikzpicture}", "")
    return code.strip()


def _balanced(code: str) -> bool:
    """花括号是否配对（不配对必然编译失败，先挡住省一次 45s 超时）。"""
    depth = 0
    for char in code:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _tail(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except Exception:  # noqa: BLE001
        return ""


@lru_cache(maxsize=1)
def get_tikz_builder() -> TikzDiagramBuilder:
    """进程级默认 TikZ 生成器（与答疑共用同一个 LLM 客户端单例）。"""
    return TikzDiagramBuilder()


def _content_rect(page: Any) -> Any:
    """一页里"真正有东西"的区域（文字块 + 图形），用来裁掉四周的空白。"""
    import pymupdf

    rects = []
    for block in page.get_text("blocks"):
        rects.append(pymupdf.Rect(block[:4]))
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is not None:
            rects.append(pymupdf.Rect(rect))
    if not rects:
        return page.rect
    union = rects[0]
    for rect in rects[1:]:
        union |= rect
    return union


def _union_rect(pdfs: list[Path]) -> Any:
    """所有帧的内容并集 —— 所有帧按同一个框裁切，逐帧之间才不会"镜头乱推"。"""
    import pymupdf

    union = None
    for pdf in pdfs:
        doc = pymupdf.open(str(pdf))
        try:
            page = doc[0]
            rect = _content_rect(page) & page.rect
            union = rect if union is None else (union | rect)
        finally:
            doc.close()
    if union is None:
        return None
    margin = 8.0                     # pt，别把线压在边上
    return pymupdf.Rect(union.x0 - margin, union.y0 - margin,
                        union.x1 + margin, union.y1 + margin)


def _pdf_to_png(pdf: Path, png: Path, dpi: int, clip: Any = None) -> None:
    """PDF 首页 → PNG（PyMuPDF，避开 poppler 依赖）。"""
    import pymupdf  # 局部导入：没装也只是画不了图，不影响服务启动

    doc = pymupdf.open(str(pdf))
    try:
        page = doc[0]
        matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        pix.save(str(png))
    finally:
        doc.close()
