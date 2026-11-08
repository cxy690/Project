"""微信客服机器人：社群维护 + 学情通知。

模块划分：

| 文件 | 职责 |
|---|---|
| ``config.py`` | 环境变量配置（没配就自动关闭对应能力） |
| ``crypto.py`` | 回调签名校验、AES 消息加解密（明文 / 安全模式） |
| ``client.py`` | access_token 缓存、客服消息、群机器人 webhook |
| ``kf.py`` | **微信客服**：拉取消息、主动发消息、欢迎语、游标持久化 |
| ``kf_service.py`` | 微信客服回调分发：拉消息 → 解析指令 → 回复 / 失败转人工 |
| ``handlers.py`` | 家长绑定、学情查询、课表、帮助 |
| ``notifier.py`` | 课后学情、周报、预警推送（家长优先走微信客服） |
| ``routes.py`` | ``/wechat/callback`` 回调入口 |

接入方式（本地开发，不需要上云）：

1. 最小可用：只配 ``WECHAT_WEBHOOK_URL``（企业微信群机器人），
   课后自动往群里发学情摘要（**广播，不能发给某一位家长**）；
2. 给家长：配 ``WECHAT_CORP_ID`` / ``WECHAT_KF_SECRET`` / ``WECHAT_TOKEN`` /
   ``WECHAT_ENCODING_AES_KEY``，家长扫码进**微信客服**，发「学情」即可查询；
   注意官方 48 小时窗口限制，课后自动推只能尽力而为；
3. 公众号：配 ``WECHAT_APP_ID`` / ``WECHAT_APP_SECRET``，家长关注后查询。

**默认全部关闭**：没配环境变量时 ``/wechat/callback`` 返回 404，
课后推送也不会发生 —— 不影响现有主应用。

注意：本包**不在这里导入 ``routes``**。路由由 ``server/app.py`` 显式 include，
这样 ``handlers`` / ``client`` 可以被脚本单独引用，不会顺带拉起 FastAPI。
"""
from wechat.config import WeChatConfig, load_config

__all__ = ["WeChatConfig", "load_config"]
