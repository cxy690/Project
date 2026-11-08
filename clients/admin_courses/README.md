# admin_courses — 管理员课程监控

页面入口：`/app/admin_courses/`。原生 HTML + 内联 JS，复用 `shared/ui.css` / `shared/ui.js`。

## 功能

- 仅管理员可访问；学生 / 家长 token 返回 403
- 列出**正在进行的 1v1 课堂**：
  - 学生、学科、老师
  - 学段 / 年级
  - 当前阶段（问候 / 讲解 / 互动 / 总结）
  - 已用 / 剩余时间
  - 已出题数、学生回合数
  - 最后活动时间、进行中 / 已超时
- 10 秒自动刷新；切后台暂停
- 断线超过 60 秒的会话自动从列表消失，避免“僵尸课堂”

## 数据源

```text
GET /api/admin/courses
```

后端 `server/course_admin_routes.py` 读取 `session_store` 中的 `SessionState`：
`course_active`、`course_started_at`、`course_deadline`、`course_phase`、
`quiz_count`、`history`、`last_active_at`。只读，不干预课堂。

已结束课程的学情报告在 `/app/learning_center/` 查看。

## 入口

- 技能管理平台顶栏「课程监控」
- 监控控制台顶栏「课程监控（管理员）」
- 学情分析页顶栏「课程监控」

## 自检

```bash
node scripts/check_frontend_js.mjs clients/admin_courses/index.html
python -m pytest tests/test_frontend_pages.py tests/test_skill_admin.py -q
```
