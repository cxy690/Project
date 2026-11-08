"""core.skills.experience：教师 Skill 的「自我更新」经验层。

设计边界（重要）：

* **只沉淀经验**：本包只往 ``data/teacher_experience/<老师key>.jsonl`` 追加经验条目；
  **永远不会改写** ``core/skills/teacher_personality`` 里的人格提示词本体 ——
  人格与平台底线永远由人写、由人改，自我更新只在其之上生长；
* **先审后用**：复盘产出默认 ``pending``，管理员批准后才 ``active`` 并注入；
  拒绝/停用/恢复都保留 append-only 审计痕迹；
* **按环境隔离**：经验是运行时学习产物，已被 ``scripts/sync_cloud.py`` 排除在
  代码同步之外，本地与云端各自学习、互不覆盖。
"""
from core.skills.experience.feedback import gain_from_verdict, record_turn_outcome
from core.skills.experience.models import KIND_LABELS, ExperienceItem
from core.skills.experience.prompt import build_experience_prompt, render_experience
from core.skills.experience.dedupe import find_duplicate_groups
from core.skills.experience.reflector import SkillReflector
from core.skills.experience.review import pending_queue, review_queue, risk_of
from core.skills.experience.store import ExperienceStore, get_experience_store

__all__ = [
    "KIND_LABELS",
    "ExperienceItem",
    "ExperienceStore",
    "SkillReflector",
    "build_experience_prompt",
    "find_duplicate_groups",
    "gain_from_verdict",
    "get_experience_store",
    "record_turn_outcome",
    "pending_queue",
    "render_experience",
    "review_queue",
    "risk_of",
]
