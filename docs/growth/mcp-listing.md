# MCP 上架物料（字段级）

> 适用于 Smithery（smithery.ai）/ AgenticMarket / Apify Store。
> 本地 stdio 服务器在 Smithery 选 "Local / STDIO" 类型。
> 本机精确配置片段：GUI「MCP 服务」页一键复制（路径按本机生成，别手抄本文的示例）。

## 基本信息

| 字段 | 填什么 |
|---|---|
| Name | mosu |
| Tagline（一句话） | 让 AI 直接读你的微信聊天数据——12 个工具，全本地，密钥不离开你电脑 |
| Category | Data / Productivity / Local-first |
| Type | Local / STDIO |
| License | MIT |
| Repository | https://github.com/baimocn/mosu |

## 描述（长版，直接粘贴）

```
墨溯（mosu）是一个本地微信数据工具。它把你电脑里的微信数据库解密成
可查的结构化数据，并通过 MCP（Model Context Protocol）开放给 AI——

装好之后，你可以对 Claude / Cursor / 任何 MCP 客户端说：

  「看看我上个月和谁聊得最多」
  「把和张三的聊天记录导成 HTML 存档」
  「我平时说话是什么风格？」
  「这张聊天里的图是什么内容？」

AI 会自己调用工具回答，不需要你手动导出、复制、粘贴。

12 个工具：数据源自检 / 会话列表 / 读消息（可按类型/方向/时间过滤）/
联系人 / 关键词搜索 / 统计（全局+单会话）/ 朋友圈 / 年报 / 画像蒸馏 /
导出（HTML/CSV/JSON/TXT）/ 图片解密查看。

为什么是本地而不是云端：
- 解密和全部计算在你的电脑上完成，没有任何数据上传；
- 数据库密钥从微信进程只读读取，不注入、不 hook、不改微信任何文件；
- AI 只拿到你问到的那一部分数据。

已知边界（诚实声明）：
- 只支持 Windows 上的微信 PC 客户端（测试于 4.1.x）；
- 需要微信登录过本机；数据库目录由首次启动向导自动定位；
- AI 画像功能需要你自己配一个大模型 key（自备，工具不代收）。
```

## 安装命令（给用户抄的通用版）

用户机器上的路径因人而异，所以给「两步版」而不给一行命令：

```bash
# 第一步：拿到代码
git clone https://github.com/baimocn/mosu.git && cd mosu

# 第二步：让墨溯自己生成配置（自动定位微信数据目录 + 校验密钥）
python cli/mosu.py init      # Windows；其余平台用 python3

# 然后把 cli/mosu.py mcp config 打印的 JSON 加进客户端的 mcpServers
python cli/mosu.py mcp config
```

> 进阶用户可以直接用 GUI：「MCP 服务」页有一键复制按钮，配置片段按本机路径生成。

## 截图清单（用 docs/shots/ 的脱敏演示图）

| 文件 | 放哪个字段 | 说明 |
|---|---|---|
| `docs/shots/01-console.png` | 首图/封面 | 控制台：分身对话 + 通道状态 |
| `docs/shots/05-profile.png` | 图 2 | 画像：统计 + 心理学维度 |
| `docs/shots/02-browse.png` | 图 3 | 数据浏览：会话/消息/图片 |
| `docs/shots/11-mcp.png` | 图 4 | MCP 服务页：工具清单 + 配置 |

> 全部走 MOSU_DEMO=1 脱敏截图（scripts/shots.py 生成，带敏感串黑名单硬检查）。
> **不要**用真实模式截图——2026-10-25 实测过，真实截图会带出联系人姓名和微信号。

## 上架前自检（发布前逐条过）

- [ ] `mosu mcp selftest` 全绿（本机 2.8s 跑完 12 工具）
- [ ] README 首屏有「5 分钟上手」段（用 README-discover.md 草案）
- [ ] LICENSE 是 MIT（已是）
- [ ] 隐私声明在 README 显眼处（已是）
- [ ] 仓库里没有真实个人信息（wxid/路径/key 已扫过，见 git log 3377675 前的清理记录）
