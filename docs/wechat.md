# 微信通道（mosu wechat）

墨溯自研的微信官方 Bot API（iLink）客户端——**零第三方依赖**，不经过任何
Node 框架（OpenClaw 等）。它只做「消息通道」：收到消息 → 墨溯 BotService
用你的风格生成回复 → 原路发回。

> **官方能力，无封号风险**：这是微信 2026-03-22 开放的 Bot API
> （`ilinkai.weixin.qq.com`），不是 Hook/注入/外挂。正常使用受官方条款保护。
> 但它拿不到你跟好友的历史聊天记录（官方隔离社交关系），
> 「历史语料」仍靠墨溯本地解密——这正是墨溯不可替代的部分。

## 命令

```bash
python cli/mosu.py wechat login      # 扫码绑定（二维码有效期 1~2 分钟，当场扫）
python cli/mosu.py wechat serve      # 前台长轮询服务（Ctrl+C 停止）
python cli/mosu.py wechat status     # 通道状态（账号/游标）
python cli/mosu.py wechat install    # 常驻：登录自启 + 崩溃自愈
python cli/mosu.py wechat task       # 常驻状态
python cli/mosu.py wechat logout     # 清除凭证/游标/上下文
```

## 铁律：一个 bot token 只能有一个消费者

同一 token 同时被两个客户端长轮询会互相吞消息。换机器/换工具前，
先把旧消费者停掉（`mosu wechat uninstall` 或停掉对方的服务）。

## 实现要点（源码在 `core/wechat.py`）

| 环节 | 做法 |
|---|---|
| 登录 | 官方二维码 → 轮询扫码状态 → token 落 `~/.mosu/wechat.json` |
| 收发 | `ilink/bot/getupdates` 长轮询（35s 超时属正常控制流，不是故障） |
| 请求头 | `AuthorizationType: ilink_bot_token` + `X-WECHAT-UIN`（随机 uint32 base64）+ `iLink-App-Id` + `iLink-App-ClientVersion` |
| 消息体 | `base_info: {channel_version, bot_agent}` |
| 回复 | `sendmessage` **必须原样带回 `context_token`**，否则消息串到错的对话窗口 |
| 游标 | `get_updates_buf` 持久化到 `wechat_sync.json`，只进不退 |
| 体验 | 收到消息先发「正在输入」，不让对方干等 3~8 秒 |
| 容错 | 超时≠故障；HTTP 4xx/5xx 才是故障 → 指数退避，连续 8 次退出让守护拉起 |
| 活性 | 每轮轮询写 `~/.mosu/wechat.heartbeat`——**判活看心跳，不看计划任务状态**（任务显示 Running 不代表进程没假死） |

## 常驻双通道

`wechat install` 首选 Windows 计划任务（登录自启 + 崩溃 2 分钟间隔自愈
×5 + 无时长限制）。**若本机策略禁止 AtLogOn 触发器**
（`Register-ScheduledTask` 报 `0x80070005` 拒绝访问，企业安全策略/
优化软件常见，一次性触发器却能建），自动降级为**启动文件夹 + VBS 守护**
（隐藏窗口运行，退出 5 秒内拉起）。

```bash
python cli/mosu.py wechat task        # 两种通道的状态都能查
python cli/mosu.py wechat uninstall   # 两种通道都会清
```

非 Windows：systemd / supervisor 守护 `python cli/mosu.py wechat serve`。

## 与 OpenClaw 的关系

如果你的机器上已有 OpenClaw 的 `openclaw-weixin` 插件并且绑定过同一个 bot：
**必须二选一**（同一 token 不能两个消费者）。墨溯可以直接迁移对方的
token（账号文件 `~/.openclaw/openclaw-weixin/accounts/*.json` 里的
`token`/`baseUrl`/`userId` 写进 `~/.mosu/wechat.json`），不用重新扫码。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 收不到消息也不报错 | token 失效后异常被吞（老代码缺陷，已修） | 看 `~/.mosu/wechat.log` 有没有 `[!] 轮询失败`；清掉重新 `login` |
| 回复串到别的对话 | `context_token` 没带回 | 用最新版 `core/wechat.py` |
| 二维码反复过期 | 有效期 1~2 分钟，链路慢 | 用 `wechat login` 直接出码当场扫，别中转 PNG |
| 注册常驻报拒绝访问 | 系统策略禁 AtLogOn | 已自动降级 startup 守护；`wechat task` 确认走的是哪条通道 |
| 心跳「从未」 | serve 没跑起来 | 看日志；确认没有第二个消费者抢 token |

## 数据文件

| 文件 | 内容 |
|---|---|
| `~/.mosu/wechat.json` | token / baseUrl / accountId / userId |
| `~/.mosu/wechat_sync.json` | 长轮询游标 |
| `~/.mosu/wechat_ctx.json` | 各会话的 context_token |
| `~/.mosu/wechat.heartbeat` | 活性时间戳 |
| `~/.mosu/wechat.log` | 运行日志（常驻时无控制台，靠它查故障） |
