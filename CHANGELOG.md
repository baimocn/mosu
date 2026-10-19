# 更新日志

本项目的重大变更都记录在此。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增

- **视觉与交互升级**（参照现代组件社区趋势，全部原生 CSS 实现，零依赖）：
  - 主按钮 Aurora 渐变流 + Shiny 扫光 + Soft Glow 悬停光晕；加载态不再抖动布局
  - 玻璃拟态（Liquid Glass）令牌；输入框聚焦品牌光环；卡片悬停抬升
  - 控制台：思考点动画（Thinking Dots）+ 回复打字机流式 + 可点击建议问题
  - 统计页：**活跃度热力图**（GitHub 式一年日历，活跃天数/最长连续天数）、
    指标磁贴化（带趋势语义配色，涨红跌绿）
  - 密钥扫描进度从"滚动条"升级为**进度环**（阶段加权 + 阶段内细分进度）
  - 空态全部换为细线 SVG 插画（随主题变色），聊天消息悬停显示复制按钮
- `strip_sender_prefix`（`core/msgparse.py`）

### 修复

- **群聊消息正文残留 `wxid_xxx:` 发送者前缀**（CLI 导出与 GUI 同时受影响），
  现已在解析层剥除；URL 冒号结尾的行不会被误伤
- 群成员拿不到昵称时界面直接显示原始 wxid，改为优雅兜底

## [0.3.0] - 2026-10

### 新增

- **密钥自动获取**（`mosu key find` / GUI 首启向导）——新电脑零门槛：
  - 只读内存扫描（`ReadProcessMemory`，不注入不 hook），微信 4.1.15.13 实测
    还原密钥与 HMAC 权威校验逐字节一致，约 30 秒
  - 原理：版本 XOR 掩码从 `Weixin.dll` 代码段静态提取（零依赖 PE 解析），
    堆内结构锚点定位候选，PBKDF2+HMAC 并行校验（hashlib 释放 GIL，8 线程）
  - 环境预检 `/api/key/selftest`（进程/dll/掩码/数据目录逐项检查）
  - 失败降级：手动填密钥 / 诊断摘要（不含敏感值）交给任意大模型分析
- **GUI 全面重做为工业级界面**（设计系统 + 应用外壳 + 组件化）：
  - 设计令牌双主题（浅/深/跟随系统），改主题不动组件
  - 侧边栏分组导航、命令面板（Ctrl+K）、Toast、骨架屏、空态、灯箱
  - 首启向导：欢迎 → 环境检测 → 一键取密钥（SSE 实时进度，可取消）→ 完成
  - 数据浏览：会话搜索/筛选高亮、按天分组、连续消息归并、向上翻页保滚动位
  - 统计/画像/年报自适应渲染（结构化递归组件，任何数据结构不白屏）
  - 设置页：密钥管理、大模型配置（URL/API Key/模型 + 连通性测试）、外观
- **大模型配置持久化**（`core/llmconfig.py`）：GUI 里填 URL/API Key/模型名，
  落 `~/.mosu/config.json`；环境变量优先；`/api/llm/test` 真实连通性测试；
  LlmClient 自动读配置（核心功能依然全离线，不需要大模型）
- `mosu key find` 子命令（`--no-save` 只看不写）
- `gui/assets/`：前端拆分模块（theme/app.css + core/views/app.js），无构建链

### 修复

- `/api/status` 的 `data.ready` 误用懒加载状态（进程刚启动必然 false，
  导致每次重启都误弹首启向导）→ 改为判定配置链（密钥+目录）
- 撤回消息显示原始 `<sysmsg>` XML → 翻译成「撤回了一条消息」等可读文本
- `/api/messages` 契约补齐：稳定 id、createTime、senderName/senderId、
  hasMore 分页元数据（前端向上翻页与归并依赖）
- 静态资源路由泛化为 `/vendor/` + `/assets/` 双目录（前端拆分后所需）

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
