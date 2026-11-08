# core/learning — 学情分析

把课堂中已经发生的事件（心跳、阶段、出题、判题、学生回合）落成 append-only
事件流，再聚合为可解释的三个指标：

| 指标 | 回答的问题 | 主要信号 |
| --- | --- | --- |
| `completion_rate` 课时完成度 | 这节课是否完整上完 | 有效在场 / 计划时长、阶段到达、出题作答 |
| `mastery` 学习掌握度 | 学生是否真的学会 | 首次作答正确率（重试答对不算首答） |
| `interaction_rate` 互动率 | 学生是否在参与 | 响应率、主动提问、有效互动 |

没有作答证据的课堂标记为「未验证」，不会因为“待满 40 分钟”就判定完成学习。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `events.py` | 采集入口：`record_session_start` / `record_heartbeat` / `record_student_turn` / `observe_session_event` / `finish_session`。 |
| `store.py` | JSONL 事件与 JSON 报告的读写；按天滚动，坏行跳过，写失败静默。 |
| `metrics.py` | 纯计算：`compute_report(events)` → 报告；`summarize_reports()` → 看板概览。 |
| `service.py` | 查询编排：单节课报告、最近报告列表、详情时间线。 |
| `__init__.py` | 对外导出。 |

当前 `store.py` 用 JSONL 落盘（与 `memory` / `verdict` 同款轻量方案），课堂主链路
不依赖 MySQL；数据量上来后只需替换 `store` 的读写实现（MySQL / ClickHouse），
`metrics` / `service` / API / 前端都不用改。

## 指标口径

### 课时完成度

```
T = 有效在场秒数 / 计划秒数
P = 到达阶段权重和（lecture 0.75 + interaction 0.25）
Q = 已作答题目数 / 已出题数
completion_rate = 0.45T + 0.35P + 0.20Q
```

- 有效在场只累计「页面可见 + 心跳间隔 ≤ 30 秒」的时间，且从学生真正点「开始上课」
  （`course_start`）之后算起，开课闸门前的等待不计入；
- `P` 的满分要求走到 `interaction`，`summary` 是否到达单独用 `summary_reached` 判断；
- 没有出题时 `Q=0`，报告会带 `no_quiz` 标记。

### 学习掌握度

```
mastery = 首次作答正确数 / 首次作答题目数
```

- 同一题重试答对不改变首答结果（按 `qid` 配对）；
- 少于 2 道首答时报告仍给值，但判定「完成学习」会要求 `first_attempts ≥ 2`；
- 完全没有作答证据时为 `null`，看板显示「未验证」。

### 互动率

```
R = 已响应 prompt 数 / prompt 总数       （ai_ask + ai_quiz）
A = 主动提问次数 / 每 10 分钟有效时长
M = 有效互动数 / prompt 总数
interaction_rate = 0.50R + 0.25·min(1, A/2) + 0.25·min(1, M)
```

「嗯 / 不知道」可以算响应，但不算有效互动；麦克风关闭后打字作答仍然算。

### 状态

| status | 含义 | 判定 |
| --- | --- | --- |
| `completed` | 完成学习 | 完成度 ≥ 0.8、首答 ≥ 2、掌握 ≥ 0.7、到达总结 |
| `attended_not_learned` | 人在心不在 | 完成度 ≥ 0.8、掌握 < 0.5 |
| `unverified` | 未验证 | 完成度够，但没有作答证据 |
| `partial` | 部分完成 | 其他有数据但未达标 |
| `abandoned` | 中途离开 | 有效在场不足一半，或断线后未收尾 |
| `in_progress` | 进行中 | 无 `session_end` 且最后事件在 5 分钟内 |

报告同时返回 `flags`（`no_quiz` / `no_answer` / `low_interaction` / `low_mastery` /
`no_summary` / `left_early` / `no_heartbeat` / `no_course_start` / `safety_alert`），便于看板做告警和人工复核。
没有心跳的旧客户端只能证明「会话跨了这么久」，时间分最多按 50% 计；没有 `course_start`
（停在开课闸门 / 只打开页面）同样封顶 50%，避免纯挂机刷出完成。

## 采集链路

```text
浏览器 ping(visible) ──────┐
SessionState.push_event ──┼─→ core.learning.events ─→ store（JSONL）
Supervisor.handle_turn ───┘                         └→ reports（JSON）
                                                       ↓
                              /api/learning/reports · /api/learning/report/{id}
                                                       ↓
                                          /app/learning_center/
```

- `push_event` 只采 `course_state` / `ai_quiz` / `ai_ask` / `ai_check` / `crisis_alert`；
- `heartbeat` 与 `course_start` 在 WebSocket 接收循环采（带页面可见性）；
- `student_turn` 在 `Supervisor.handle_turn` 收口采（带 `is_answer`，区分作答与主动提问）；
- `session_end` 在连接关闭时由 `finish_session` 写入并生成报告。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `LEARNING_ANALYTICS_ENABLE` | `true` | 总开关；关闭后不写任何事件 |
| `LEARNING_DIR` | `data/learning` | 事件与报告目录（运行时产物，已 gitignore） |
| `LEARNING_RETENTION_DAYS` | `30` | 看板默认回看天数 |
| `LEARNING_HEARTBEAT_TIMEOUT_SECONDS` | `30` | 心跳间隔超过该值不计有效在场 |

## 隐私

- 事件只保存学生输入的**截断文本**与结构化统计，不保存音频、图像 base64、白板指令；
- 不采集摄像头画面或人脸注意力；
- `data/learning/` 含学生输入原文，已加入 `.gitignore`，按机构数据保留策略清理；
- 学生 / 家长只能查看自己的报告；教师 / 管理员可按学生筛选。

## 自检

```bash
python -m pytest tests/test_learning_analytics.py tests/test_frontend_pages.py -q
node scripts/check_frontend_js.mjs clients/learning_center/index.html
```
