# user_center — 用户中心（学段设置）

页面入口：`/app/user_center/`。原生 HTML + 内联 JS，无构建流水线，复用 `shared/ui.css` / `shared/ui.js`。

## 功能

- 读取 `localStorage["edu_auth"]`，未登录跳转 `/app/login/?next=/app/user_center/`
- `GET /api/auth/me` 展示用户名、身份、年级、当前学段
- 小学 / 初中二选一，点击「保存学段」调用 `PATCH /api/auth/me/stage`
- 保存成功后同步回写 `edu_auth.user.stage`，并提示「下次进入课堂生效」
- 账号区支持退出登录 / 切换账号

## 生效链路

```text
用户中心保存
  -> PATCH /api/auth/me/stage
  -> MySQL users.stage = primary | junior
  -> localStorage edu_auth.user.stage 同步
  -> 下一次 WebSocket 建连读取数据库 stage
  -> 加载 .agents/skills/<学科>/<stage>/SKILL.md
```

当前已建立的课堂会话不切换学段，避免讲到一半老师人格 / 语速突然变化。

## 入口

- 登录页「已登录」面板：用户中心
- 学科选择页右上角账号菜单：用户中心
- 课堂页「课堂设置」：用户中心 · 切换学段
- 首页导航：用户中心

## 自检

```bash
node scripts/check_frontend_js.mjs clients/user_center/index.html
python -m pytest tests/test_frontend_pages.py tests/test_stage_skills.py -q
```
