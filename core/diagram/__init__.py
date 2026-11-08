"""教学图（讲解时同步画出来的示意图）规格生成层。

两条出图引擎，规则都不在 Python 里：

- :mod:`core.diagram.spec`：模型产 JSON 规格，浏览器现场画 SVG
  （引擎 ``clients/vendor/teach-diagram/engine.js`` 源自 ``js-teaching-diagram`` 技能）
- :mod:`core.diagram.tikz`：模型产 TikZ 源码，编译成 PNG 帧；提示词、导言区、
  静态闸门的事实源是 ``.agents/agent_skills/tikz-teacher-diagram/``，
  由 :mod:`core.diagram.skill` 按 mtime 加载

Python 这边只负责把一道题变成规格 / 帧。选型与降级原因见各模块 docstring。
"""
from __future__ import annotations

from core.diagram.spec import DiagramSpecBuilder, get_diagram_builder
from core.diagram.tikz import TikzDiagramBuilder, get_tikz_builder

__all__ = [
    "DiagramSpecBuilder",
    "TikzDiagramBuilder",
    "get_diagram_builder",
    "get_tikz_builder",
]
