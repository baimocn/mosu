# 墨溯增长试点包（3 天）

> 本目录是「智能原生案例借鉴」结论的落地物料。主缺口：**MCP 上架 + Free/Pro 分层**。
> 分工：文案/物料/数据表我来做（已完成）；账号注册、收款配置、点「发布」按钮是你的
> human_only 节点，每处都标了 ⬜ 待你操作。

## 3 天计划总览

| 天 | 动作 | 执行方 | 状态 |
|---|---|---|---|
| Day 1 | 定分层（pricing.md）+ 备物料（本目录全部文件） | AI | ✅ 已完成 |
| Day 2 | 上架 Smithery/AgenticMarket + 闲鱼挂 Pro 授权 + 发首发帖 | 你 ⬜ | 待操作 |
| Day 3 | 填 tracker.csv，对照基准做 go/no-go | 你 ⬜ + AI 辅助 | 待操作 |

## 文件清单

| 文件 | 用途 | 给谁看 |
|---|---|---|
| `pricing.md` | Free/Pro 分层、定价、边界（含为什么不做技术锁定） | 你决策 |
| `mcp-listing.md` | 上架字段级文案：名称/描述/安装命令/分类/截图清单 | 上架表单 |
| `README-discover.md` | 仓库 README 的发现向改写草案（**未合入**，待你点头） | 来逛仓库的人 |
| `launch-post.md` | 首发帖主稿（DEV.to 中文版）+ 知乎/V2EX 变体要点 | 首发渠道 |
| `xianyu-listing.md` | 闲鱼「墨溯 Pro 授权」商品文案 | 闲鱼发布 |
| `tracker.md` / `tracker.csv` | 数据回流表 + Day 3 判定规则 | 你每天填 |
| `../shots/*.png` | 脱敏演示截图（上架和帖子用；**勿用真实模式截图**） | 全部物料 |

> 截图说明：growth 物料统一引用 `docs/shots/` 的 MOSU_DEMO 脱敏图
> （scripts/shots.py 生成，出图前过敏感串黑名单）。2026-10-25 实测：
> 真实模式截图会带出联系人姓名与微信号（browse 页）——所以没有 assets/ 目录，
> 所有图都走脱敏管线。

## 两个必须先知道的事实

1. **MCP 生态现在是早期 App Store**：11,000+ 服务器、变现率不足 5%、市场分成 80-95%
   （来源：dev.to/krisying、clawdbytes，2026-10-24 采集）。窗口真实存在，但不等人。
2. **本地工具别做技术锁定**： TypingMind 用荣誉制 license key 做到 $1M ARR。墨溯的 Pro
   卖的是「预设数据包 + 支持服务 + 更新通道」，不是破解不掉的锁——理由写在 pricing.md。

## Day 2 操作顺序（照做即可，约 90 分钟）

```bash
# 1. 确认 MCP 自检还是绿的（30 秒）
cd PROJECT_DIR && python cli/mosu.py mcp selftest

# 2. 上架 Smithery（需要 GitHub 登录，5 分钟）
#    打开 https://smithery.ai  → New Server → 按 mcp-listing.md 的字段填
#    本地 stdio 服务器选 "Local/STDIO" 类型，粘贴 GUI「MCP 服务」页生成的配置

# 3. 闲鱼挂商品（10 分钟）：文案直接抄 xianyu-listing.md，价格先挂 ¥68 买断

# 4. 发首发帖（20 分钟）：launch-post.md 主稿发 DEV.to，知乎/V2EX 用变体要点改写
```

> 若日后要换新截图：先 `python scripts/demo_server.py --port 8790`，再
> `MOSU_SHOT_BASE=http://127.0.0.1:8790 python scripts/shots.py`——黑名单不过不出图。

## Day 3 判定规则（防拍脑袋）

打开 `tracker.md`，按里面的基准线做 go/no-go。核心一条：**7 天零安装不改产品，
改物料**——出口问题用出口解决。
