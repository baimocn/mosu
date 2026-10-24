# README 发现向改版草案（三处定向增强，未合入）

> 现有 README 质量不错，不做重写。以下三处是按「发现场景」优化的定向修改：
> 陌生人从 GitHub/MCP 市场点进来，前 30 秒要看到的是「AI 能直接读我的微信数据」，
> 而不是解密原理。原理留在「为什么做这个」，不放首屏。
> 用法：三处原文/替换内容都用缩进引用块给出，合入时逐块替换即可。

---

## 修改 1：首屏一句话

**原文**（README.md 第 3-11 行）：

    > 本地微信数据解密与导出工具 —— 自有 SQLCipher 4 引擎，零 DLL、零网络、零授权检查。

    一句话：把微信电脑端那些打不开的 `.db` 文件，变成你能读、能搜、能统计、能导出的数据。

    ```bash
    pip install -r requirements.txt
    python cli/mosu.py init --db-path "<微信数据目录>" --key <64位hex>
    python cli/mosu.py sessions
    ```

**替换为**：

    > 本地微信数据工具 —— 解密、统计、导出，并通过 MCP 让 AI 直接读。

    一句话：把微信电脑端那些打不开的 `.db` 文件，变成你能读、能搜、能导出的数据；
    再进一步——对 AI 说「看看我上个月和谁聊得最多」，它自己调用工具回答。

    ```bash
    # 30 秒看到第一条 AI 读到的数据（需要 Python 3.10+）
    pip install -r requirements.txt
    python cli/mosu.py init          # 向导自动定位数据目录 + 校验密钥
    python cli/mosu.py mcp selftest  # 12 个工具全部真跑一遍
    ```

---

## 修改 2：在 `## 界面` 章节之前，插入「让 AI 用起来」整段

**插入内容**：

    ## 让 AI 用起来（MCP）

    墨溯同时是一个 MCP server：任何支持 MCP 的客户端（Claude Desktop / Cursor /
    WorkBuddy）加上一段配置，就能用自然语言查询你的聊天数据：

    | 你对 AI 说 | 背后发生的 |
    |---|---|
    | 「看看我上个月和谁聊得最多」 | mosu_stats + mosu_sessions |
    | 「把和张三的聊天导成 HTML 存档」 | mosu_export |
    | 「我平时说话是什么风格？」 | mosu_profile |
    | 「这张聊天截图里的图是什么」 | mosu_media（本地解密图片） |

    配置方法：跑 `python cli/mosu.py mcp config`，把打印的 JSON 加进客户端的
    mcpServers；或用 GUI「MCP 服务」页的一键复制（路径按本机生成）。
    12 个工具清单见 [docs/CLI.md](docs/CLI.md)。

    数据边界：AI 只拿到你问到的那部分；密钥与数据库不离开本机。

---

## 修改 3：界面区演示截图之后，补两张

**插入内容**：

    <p align="center">
      <img src="docs/shots/01-console.png" width="440" alt="控制台：数字分身对话与通道状态">
      <img src="docs/shots/11-mcp.png" width="440" alt="MCP 服务页：工具清单与一键配置">
    </p>

两张都走现有 MOSU_DEMO 脱敏管线（scripts/shots.py，带黑名单硬检查），
与 README 里其他演示截图同源，不需要额外标注。

## 合入方式

你点头后我直接改 README.md（约 +40 行，不动现有章节结构）。
唯一应该撤掉的情况：你决定不公开 MCP 入口——那把修改 2 单独撤掉，1 和 3 保留。
