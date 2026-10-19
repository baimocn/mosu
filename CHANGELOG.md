# 更新日志

本项目的重大变更都记录在此。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] - 2026-10

### 新增

- **GUI 本地控制台**（`mosu gui`）：浏览器打开 `127.0.0.1:8788`，五个标签页
  - 控制台：数字分身对话调试 + 微信消息流水 + 状态详情
  - 数据浏览：三栏（会话列表 / 微信气泡消息流 / 全库搜索），
    聊天图片解密直出（`/api/media?md5=`）
  - 数据统计：全局卡片 + 小时/星期/月分布 + 联系榜 + 关键词 Top30
    （自绘 SVG 图表，不引图表库）
  - 朋友圈：正文/图片/点赞/评论信息流，offset 分页
  - 导出：HTML zip / JSON / CSV / TXT，异步任务 + 耗时回显
  - 常驻双通道：Windows 计划任务优先，AtLogOn 被策略禁时自动降级
    启动文件夹 + VBS 守护
- **微信通道**（`mosu wechat`）：微信官方 Bot API（iLink）自研客户端，
  扫码登录 / 长轮询收发 / 游标持久化 / context_token 管理 / 心跳判活 /
  常驻安装（`login/serve/status/install/uninstall/task`）
- 数字分身 bot（`mosu bot`）：云端 LLM（OpenAI 兼容，任意后端）+
  RAG 风格锚点 + 规则硬校验（AI 腔/长度/感叹号/照抄检测），
  端到端消融验证三组件缺一不可
- `Moments.entries()` 支持 offset 分页与 `meta.cost` 耗时回传
- `Dataset.collect_session_rows()`：跨分片消息收集归位数据层
  （CLI 与 GUI 导出共用）

### 修复

- SQLite 跨线程崩溃：多线程 HTTP + 懒加载 Dataset 时连接被跨线程复用，
  `conn()` 加 `check_same_thread=False` + 数据 API 全程 RLock 串行
- VBS 守护脚本 `Set q = Chr(34)` 类型不匹配静默崩 → `q = Chr(34)`
- PowerShell 5.1 输出 GBK 导致 Python subprocess 读线程 UnicodeDecodeError
  → `encoding="utf-8", errors="replace"`
- `wechat get_updates` 曾把异常全吞（token 失效时假装在跑），
  现区分「超时=正常控制流」与「4xx/5xx=真故障」
- `bot` prompt 曾把风格约束与知识边界混在一条规则里
  （语料外话题一律答「不知道」），已解耦；长度校验上限 80→200

### 安全

- GUI 导出/静态资源防目录穿越（`resolve()` + `startswith`，
  `../` 与 `..%2f` 均 404）；导出产物目录固定，无任意文件读写
- 前端消息文本全走自动转义；SVG 图表用 `v-for` 渲染不拼 HTML 字符串
- `.env`、`persona/`（真实语料）、`.cache/`、`export/` gitignore

### 测试

- 离线测试 37 → 90 例（新增 GUI 导出序列化 / zip / TaskHub 覆盖）

## [0.1.0] - 2026-10

### 新增

- 自有 SQLCipher 4 解密引擎（PBKDF2-HMAC-SHA512 × 256000，
  AES-256-CBC + HMAC-SHA512），零 DLL、零网络、零授权检查
- 消息/联系人/群/语音/视频/朋友圈全格式解析，多分片自动合并去重
- CLI 13 组命令 + 五种输出格式（json/jsonl/table/csv/markdown）
- MCP Server（12 工具，stdio）
- 画像蒸馏（全库画像 + 单联系人深度画像）+ 离线中文情感分析
- HTML 气泡导出（图片视频内嵌）+ 增量导出 + 多账号切换
- 媒体解密：图片 V0（XOR）与 V2（私有容器 AES-128-ECB + 尾部 XOR）、
  语音 SILK → MP3
