# .agents — 数字人教师技能（运行期事实源）

教师技能（teacher skill）的落点。**运行期事实源在 `skills/`**，设计稿与维护笔记在 `notes/`（运行期不读）。

```
.agents/
├── skills/                        # ★ 一科一目录，运行期从 SKILL.md 现读
│   ├── chinese/
│   │   ├── SKILL.md               #   学科默认技能（学段版缺失时兜底）
│   │   ├── stages/
│   │   │   ├── primary.md         #   小学学段专属（高专业度，独立人格）
│   │   │   └── junior.md          #   初中学段专属
│   │   └── references/            #   流派设计稿（人读，不注入提示词）
│   ├── math/  english/ science/ morality/ geography/ …
│   ├── physics/ chemistry/ biology/ politics/ history/   # 只写 junior
└── notes/                         # 设计稿 / 维护笔记
    ├── README.md                  #   完整目录说明与 SKILL.md 格式规范
    └── teacher-skills-plan.md     #   教师技能总体方案（风格 / 语速 / 深度三维度）
```

## 文件说明

| 路径 | 用途 |
| --- | --- |
| `skills/<学科key>/SKILL.md` | 一份 Markdown 同时承载**两层技能**：frontmatter 给风格 / 深度 / 语速（`config/teacher_skills` 消费）；正文给人格与教学行为（`core/skills/teacher_personality` 消费）。 |
| `skills/<学科key>/stages/<stage>.md` | **学段专属技能**：`primary`（小学）/ `junior`（初中）。同一学科小学与初中是两份独立人格（不同 skill_id、不同讲课方式、不同 depth）。会话按 `scratch["grade"]` 选，识别不出时回退 `SKILL.md`。 |
| `skills/<学科key>/references/*.md` | 流派设计稿（如 `math-modeling-studio.md`、`misconceptions.md`），供人阅读与校准技能，**不注入提示词**。 |
| `notes/README.md` | 目录与格式的权威说明（frontmatter 字段、两层的消费方、维护流程）。 |
| `notes/teacher-skills-plan.md` | 教师技能总体方案（设计背景与三维度规划）。 |

## 设计要点

- **改 md 即生效**：运行期按 mtime 失效缓存现读现改，线上调整教师风格不需要重启或改代码。
- **小学 / 初中是两套技能**：`stages/primary.md` 与 `stages/junior.md` 各自独立（学段目标、认知负荷、讲法、depth 都不同），按学生年级自动切换。只写一科也能工作 —— 没写学段版的学科仍用 `SKILL.md`，行为与拆分前一致。
- **只给真开课的学段写**：小学没有物理/化学/生物/历史/政治，初中没有独立的「科学」；写之前先看 `config/subjects.py` 的 `grades`（`tests/test_stage_skills.py` 会拦）。
- **目录名 = 学科 key**：与 `config/subjects.py` 的 `key`、`data/teachers/<key>/` 三者一致；少一个目录 = 该科没有技能，回退 `teacher.json` 的 `persona`。
- **人写人格、机器学经验**：本目录的人格内容由人维护（**永不被程序改写**）；模型学习到的「经验」单独落在 `data/teacher_experience/`（见 `core/skills/experience/`）。
