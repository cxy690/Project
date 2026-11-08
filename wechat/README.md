# 微信客服机器人

维护学习社群 + 推送学情通知。**全部能力默认关闭**，没配环境变量时不影响主应用。

## 一、三种接法

### A. 只发通知（最省事，5 分钟可用）

企业微信群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址：

```bash
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
```

效果：每节课结束，自动往群里发一条学情摘要（完课度 / 掌握度 / 互动率 / 待关注项）。

> 群机器人是**广播**：一条消息群里所有人可见，**不能只发给某一位家长**。
> 学情属于未成年人数据，别把通知群和业务群混在一起。

### B. 微信客服（家长扫码即可，推荐）

家长扫客服二维码就能聊，**不用关注公众号、不用加好友**。

开通（企微管理后台）：

1. 应用管理 → 微信客服 → 开通；
2. 微信客服 → API → 复制 Secret（填 `WECHAT_KF_SECRET`）；
3. 微信客服 → 可调用接口的应用 → 把自建应用加进去（不加会报 `48002`）；
4. 自建应用 → 接收消息 → 设置回调 `https://<固定域名>/wechat/callback`，
   Token / EncodingAESKey 与 `.env` 填一致。

```bash
WECHAT_TOKEN=随机字符串              # 回调签名 token（后台填同一个）
WECHAT_ENCODING_AES_KEY=43位字符串    # 企微后台默认加密模式，必填
WECHAT_CORP_ID=企业 CorpID
WECHAT_KF_SECRET=微信客服 Secret
WECHAT_KF_OPEN_KFID=                 # 可留空，自动取账号列表第一个
WECHAT_BIND_SECRET=班主任发给家长的验证码
```

**48 小时窗口（微信硬规则，代码绕不过）**

> 仅当微信客户在主动发送消息给客服后的 48 小时内，企业可发送消息给客户，
> 最多可发送 5 条消息。

所以三条路径的可靠性完全不同：

| 路径 | 是否可靠 | 实现 |
|---|---|---|
| 家长发「学情」 | ✅ | `sync_msg` 拉到后立刻 `send_msg` |
| 家长点开客服窗口 | ✅ | `enter_session` → 用 `welcome_code` 发欢迎语，直接给最近一节课 |
| 课后自动推 | ⚠️ | 家长 48 小时内发过消息才成功；失败走 `msg_send_fail`（`fail_type=4`），转群机器人提醒老师 |

### C. 公众号 / 企微应用回调（可收可发）

公众号后台或企微自建应用配置回调地址 `https://<你的域名>/wechat/callback`，
再配：

```bash
WECHAT_TOKEN=随机字符串              # 回调签名 token（后台填同一个）
WECHAT_ENCODING_AES_KEY=43位字符串    # 安全模式才需要；明文模式留空
WECHAT_APP_SECRET=应用 Secret
WECHAT_APP_ID=公众号 AppID           # 公众号
# 或企业微信
WECHAT_CORP_ID=企业 CorpID
WECHAT_AGENT_ID=应用 AgentId
WECHAT_BIND_SECRET=班主任发给家长的验证码
```

> 企微**自建应用**只能发给通讯录成员（老师、教务），发给家长会失败；
> 家长场景请走 B（微信客服）或 C 的公众号分支。

> 安全模式（配置了 `WECHAT_ENCODING_AES_KEY`）需要 `pip install cryptography`；
> 没装时回调会明确报错，不会静默失败。群机器人 webhook 不需要加密依赖。

### D. 按群推送（课后 + 周报）

配了 `data/wechat/groups.json` 后，推送从「一个全局 webhook」变成「按群路由」。
样例见 `wechat/groups.example.json`，复制后改成自己的 webhook 与学生 id：

```json
{
  "groups": [
    {"id": "g_xiaoming", "name": "小明家长群", "kind": "parent",
     "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
     "student_ids": ["stu-1"]},
    {"id": "g_class_3a", "name": "初三A班家长群", "kind": "class",
     "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=yyy",
     "student_ids": ["stu-1", "stu-2", "stu-7"]}
  ]
}
```

| kind | 课后 | 周报 |
|---|---|---|
| `parent`（一对一家校群） | 该生本节明细 | 该生本周明细 |
| `class`（班级群，多个家长） | **只发「已生成，请私聊查看」，不含任何分数** | **只发班级聚合概览，不点名** |

班级群的隐私口径（硬约束）：

* 单个学生的完课度 / 掌握度 / 互动率 / 待改进项**一律不进群**（其他家长可见）；
* 班级概览的「平均分」有样本下限（`WECHAT_CLASS_WEEKLY_MIN_STUDENTS`，默认 3 人）；
  不足 3 人只报节数与时长 —— 否则「平均分」等于个人分数，一眼反推；
* 明细始终走家长私聊（微信客服 / 公众号），群里只留一句「私聊查看」。

开关：

```bash
WECHAT_NOTIFY_LESSON_END=true          # 课后推送总开关
WECHAT_MIN_LESSON_SECONDS=300          # 有效学习不足 5 分钟不推（断线重连的碎片课）
WECHAT_NOTIFY_WEEKLY=true              # 周报总开关（默认关）
WECHAT_CLASS_WEEKLY_MIN_STUDENTS=3     # 班级周报展示平均分的最少上课人数
```

> 只要 `data/wechat/groups.json` 存在，就**不再**用 `WECHAT_WEBHOOK_URL` 全局广播
> （否则班级群会收到明细）；删掉这个文件即回到旧行为。

防重：同一 `lesson_id`、同一群同一周只推一次（`data/wechat/pushed.json`），
断线重连不会把一节课发两三条。

## 二、家长可用指令

| 指令 | 效果 |
|---|---|
| `学情` | 最近一节课的学情摘要 |
| `学情 全部` | 最近 5 节 |
| `课表` | 当前课程包已用时长 / 续费次数 |
| `绑定 学生ID 验证码` | 绑定孩子（验证码 = `WECHAT_BIND_SECRET`） |
| `解绑` | 取消绑定 |
| `帮助` | 指令说明 |

微信客服里点菜单「查学情 / 查课时 / 看帮助」等同于发对应指令
（`kf.send_menu` 下发，微信回传 `text.menu_id`，由 `kf_service` 映射）。

## 三、本地自测（不接微信）

```bash
# 1. 起服务
python -m server.app

# 2. 未配置 token 时回调应为 404
curl "http://127.0.0.1:8001/wechat/callback?echostr=hi"

# 3. 配置 token 后验签
set WECHAT_TOKEN=test-token
set WECHAT_APP_SECRET=secret
# 用 python 算 signature 后访问 /wechat/callback?signature=..&timestamp=..&nonce=..&echostr=hi

# 4. 直接测消息处理（不经过 HTTP）
python -c "from wechat import handlers; print(handlers.handle_text('openid-1','帮助'))"

# 5. 微信客服接入自检（会真实调企微接口，只读不发给家长）
python scripts/kf_selftest.py
python scripts/kf_selftest.py --send wmXXXXXX   # 发一条测试消息
```

`kf_selftest.py` 依次验证：配置齐全 → 换 token → 列客服账号 →
游标与最近消息。第 3 步失败基本就是「自建应用没加进可调用接口的应用」。

## 四、数据与隐私

* 绑定关系落 `data/wechat/bindings.json`（openid → 学生 id），**不进云端同步**；
* 群配置 `data/wechat/groups.json`、推送去重 `data/wechat/pushed.json` 同目录，也不同步
  （不同环境对应不同群，每台机器各配一份）；
* 班级群只收「已生成，请私聊查看」与班级聚合概览，**单个学生的分数不外发**；
* `解绑` 立即删除该 openid 的关联；
* 学情内容只来自 `core/learning_stats`，不额外复制学生原始对话。

## 五、接入点

| 位置 | 作用 |
|---|---|
| `server/app.py` | `app.include_router(wechat_router)`（必须在静态兜底之前） |
| `server/websocket.py` | 下课时 `schedule_lesson_report(metrics)` 异步推送 |
| `wechat/groups.py` | 群注册表：学生 ↔ 群、`parent` / `class` 两类口径、班级群称呼脱敏 |
| `wechat/notifier.py` | `push_lesson_report` / `push_weekly` / `push_weekly_all` / `push_alert`（含防重） |
| `wechat/kf.py` | 微信客服客户端：`sync_msg` / `send_msg` / 欢迎语 / 游标持久化 |
| `wechat/kf_service.py` | 回调分发：拉消息、菜单映射、`msg_send_fail` 转人工 |
| `scripts/kf_selftest.py` | 接入自检（配置 / token / 账号 / 消息） |
| `scripts/push_weekly_reports.py` | 周报定时入口（`--dry-run` 预览 / `--week` 补发 / `--group` 限群） |

周报需要外部定时触发（项目没有 scheduler，与既有约定一致）：

```bash
python scripts/push_weekly_reports.py --dry-run   # 先看文案，不发
python scripts/push_weekly_reports.py             # 发本周（周一 ~ 现在）
python scripts/push_weekly_reports.py --week 2026-10-14   # 补发某一周
```

云端 crontab（每周日 20:00）：

```bash
0 20 * * 0 cd /root/autodl-tmp/education && .venvs/app/bin/python scripts/push_weekly_reports.py >> logs/weekly_push.log 2>&1
```
