# wechat_admin — 微信助手管理台

页面入口：`/app/wechat_admin/`。仅管理员可访问。

## 功能

- 家长 ↔ 学生绑定（一个家长可绑定多个孩子）
- 家长通知目标：企业微信群机器人 webhook / Server酱 SendKey / mock
- 目标地址脱敏展示，不在列表回显完整 webhook 或密钥
- 今日日报预览（按学生 + 日期）
- 单条手动发送、立即运行今日批量发送
- 发送记录：已发送 / 失败 / 已跳过 + 错误原因
- 每天 30 秒刷新发送记录；页面可见时自动刷新

## 接口

```text
GET    /api/admin/wechat/status
GET    /api/admin/wechat/options
GET    /api/admin/wechat/bindings
POST   /api/admin/wechat/bindings
DELETE /api/admin/wechat/bindings?guardian_id=&student_id=
GET    /api/admin/wechat/preview?student_id=&date=
POST   /api/admin/wechat/send
POST   /api/admin/wechat/run
GET    /api/admin/wechat/deliveries
```

全部要求管理员 token。

## 渠道说明

- `mock`：写 `data/wechat/outbox/<日期>/*.json`，用于本地演示，不会真的发微信。
- `wecom_webhook`：企业微信群机器人；目标可填群 webhook，留空时用
  `WECHAT_WECOM_WEBHOOK` 兜底。
- `serverchan`：Server酱 SendKey，可推送到个人微信；留空时用
  `WECHAT_SERVERCHAN_KEY` 兜底。

不接入个人微信自动化协议，避免账号风险。

## 自检

```bash
node scripts/check_frontend_js.mjs clients/wechat_admin/index.html
python -m pytest tests/test_wechat_assistant.py tests/test_frontend_pages.py -q
```
