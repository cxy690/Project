# learning_center — 学情分析看板

页面入口：`/app/learning_center/`。原生 HTML + 内联 JS，复用 `shared/ui.css` / `shared/ui.js`。

## 功能

- 最近课堂列表：学生、学科/学段、有效时长、课时完成、学习掌握、互动率、状态
- 顶部概览：课程数、完成学习数、平均完成度、平均掌握度、平均互动率
- 单节课详情：阶段、出题/作答、首答正确、学生回合、主动提问、异常标记、事件时间线
- 状态口径：`完成学习 / 人在心不在 / 未验证 / 部分完成 / 中途离开 / 进行中`
- 学生 / 家长只看到自己；教师 / 管理员可按学生 ID 筛选
- 页面可见时每 30 秒静默刷新，切后台暂停

## 数据来源

```text
GET /api/learning/reports?student_id=&days=&limit=
GET /api/learning/report/{session_id}
```

指标计算在 `core/learning/metrics.py`，事件采集在 `core/learning/events.py`。

## 自检

```bash
node scripts/check_frontend_js.mjs clients/learning_center/index.html
python -m pytest tests/test_learning_analytics.py tests/test_frontend_pages.py -q
```
