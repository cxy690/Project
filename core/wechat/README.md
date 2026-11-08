# core/wechat — 微信助手 / 家长日报

每天到点后，把学生在 AI 课堂当天的学情汇总成一条家长可读的消息，按
`guardian_links` 的家长-学生关系发送。

## 设计边界

- **只读学情**：数据来自 `core/learning` 的课堂事件/报告，不改课堂链路。
- **不改写教师技能**：日报只是汇总，不参与 SKILL.md / 经验审核。
- **默认 mock**：本地只写 `data/wechat/outbox`，不会误发外部消息。
- **官方/合规通道**：企业微信群机器人 webhook、Server酱 SendKey；
  不接入个人微信 hook / 协议号，避免封号与合规风险。

## 流程

```text
每天 WECHAT_REPORT_TIME（默认 20:30，Asia/Shanghai）
  -> 遍历 guardian_links
  -> reports_for_date(student, today)
  -> build_daily_report()
  -> WeChatTarget（家长通知目标）
  -> provider.send_message()
  -> wechat_deliveries 幂等记录
```

幂等键是 `(report_date, guardian_id, student_id)`：同一天重复运行/手动重发，
默认跳过；管理台可显式 `force` 重发。

## 文件

| 文件 | 作用 |
| --- | --- |
| `bindings.py` | 家长-学生监护关系、家长微信目标（webhook / SendKey）的增删查，目标脱敏 |
| `report.py` | 按学生 + 本地日期筛选报告，汇总成日报文本与结构化 summary |
| `providers.py` | `mock` / `wecom_webhook` / `serverchan` 发送适配器 |
| `service.py` | 预览、单条发送、当天批量发送、发送记录、状态统计 |
| `scheduler.py` | 应用生命周期内的每日定时循环 |

## 家长权限

家长登录后访问 `/api/learning/reports` 不传 `student_id` 时，服务端按
`guardian_links` 解析孩子；如果绑定了多个孩子，必须显式指定 `student_id`。
未绑定时返回 403。

## 配置

```env
WECHAT_ASSISTANT_ENABLE=true
WECHAT_SCHEDULER_ENABLE=true
WECHAT_REPORT_TIME=20:30
WECHAT_REPORT_TIMEZONE=Asia/Shanghai
WECHAT_PROVIDER=mock
WECHAT_DIR=data/wechat
WECHAT_WECOM_WEBHOOK=
WECHAT_SERVERCHAN_KEY=
WECHAT_PUBLIC_BASE_URL=
```

`WECHAT_PUBLIC_BASE_URL` 留空时使用 `http://127.0.0.1:<PORT>`，线上改成公网地址，
否则家长点「查看详情」会打不开。

## 管理入口

```text
/app/wechat_admin/
```

支持：家长/学生绑定、目标脱敏展示、日报预览、单条发送、立即运行今日日报、发送记录。

## 测试

```bash
python -m pytest tests/test_wechat_assistant.py tests/test_frontend_pages.py -q
```
