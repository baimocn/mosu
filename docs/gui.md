# GUI 控制台（mosu gui）

本地 Web 控制台。一条命令起服务、自动开浏览器，**纯 Python 标准库 HTTP**——
不需要 Node、不需要构建链、不依赖 CDN（Vue3 运行时随仓库分发在 `gui/vendor/`）。

```bash
python cli/mosu.py gui                  # 起服务并打开 http://127.0.0.1:8788
python cli/mosu.py gui install          # 常驻：登录自启 + 崩溃自愈
python cli/mosu.py gui task             # 查看常驻状态
python cli/mosu.py gui uninstall        # 卸载常驻
```

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `MOSU_GUI_PORT` | 监听端口 | `8788` |

只绑 `127.0.0.1`（个人工具，不对外）。端口已被占用时会直接打开浏览器，
不会起第二份实例。

## 五个标签页

| Tab | 内容 |
|---|---|
| 控制台 | 对话框调试数字分身（回复带耗时/尝试次数/校验状态/检索参考）+ 微信消息流水 + 状态详情 |
| 数据浏览 | 三栏：会话列表（类型过滤/搜索）\| 微信气泡消息流（图片解密直出）\| 全库搜索 |
| 数据统计 | 全局卡片 + 小时/星期/月分布 + 联系榜 + 关键词 Top30 |
| 朋友圈 | 正文/图片/点赞/评论信息流，「加载更多」分页 |
| 导出 | 选会话 + 格式（HTML zip / JSON / CSV / TXT）+ 时间范围 → 下载 |

`?tab=browse&session=<wxid>` 可作为书签直达某个标签/会话
（`tab` 取值：`browse` / `stats` / `moments` / `export`）。

## API 端点

所有错误都返回 JSON `{"error": "..."}`：参数错 → 400，不存在 → 404，
其余 → 500。控制台端点（`/api/status`、`/api/log`、`/api/chat`）**不持数据锁**，
重统计/导出执行期间控制台始终可响应。

| 端点 | 方法 | 参数 | 说明 |
|---|---|---|---|
| `/api/status` | GET | — | 通道/服务/数据源状态 |
| `/api/log?n=50` | GET | n≤200 | 微信消息流水（`[收到]`/`[回复]`/`[!]`） |
| `/api/chat` | POST | `{message}` | 数字分身决策（reply/tries/cost/passed/retrieved） |
| `/api/sessions` | GET | `type/q/offset/limit` | 会话列表 |
| `/api/messages` | GET | `session&limit&offset` | 会话消息（limit≤200） |
| `/api/search` | GET | `q&limit` | 全库搜索 |
| `/api/media` | GET | `md5` | 解密图片二进制（`<img src>` 直接渲染） |
| `/api/stats/global` | GET | `from?/to?` | 全局统计（含 `deduped:false` 口径标志） |
| `/api/stats/contacts` | GET | `top`（1~100）, `from?/to?` | 联系榜 |
| `/api/stats/time` | GET | `by=hour\|weekday\|month\|date`, `from?/to?` | 时间分布（hour 恒 24 桶、weekday 恒 7 桶） |
| `/api/stats/keywords` | POST | `{top, max_msgs}`（max_msgs 1000~20000） | 关键词统计（**异步任务**，全库扫描数十秒） |
| `/api/profile` | GET | `session?`, `async?`, `from?/to?` | 画像（`async=1` → **异步任务**，kind=`profile`） |
| `/api/moments` | GET | `limit&offset&user` | 朋友圈（limit≤50） |
| `/api/export` | POST | `{session, format, from?, to?}` | 导出（**异步任务**） |
| `/api/task` | GET | `id` | 任务状态（queued/running/done/error + cost + result） |
| `/api/export/download` | GET | `task` | 任务完成后流式下载（中文文件名 RFC 5987） |

### 重操作与锁

数据 API 全程持一把 `RLock`（SQLite 连接不跨线程并发用），
所以数据面板之间是串行的。关键词统计和 HTML 导出是重操作
（全库逐条解压 / 逐张解密媒体），它们跑在独立 worker 线程的**异步任务**里，
前端 1 秒轮询 `/api/task` 显示耗时；控制台端点不持锁，任何时候都能用。

`/api/profile` 的 `?async=1` 是**可选**的：不带该参数仍走同步旧路径（CLI / MCP
间接依赖 `ProfileDistiller` 的同步语义，一次全切爆炸半径不值）。联系人画像另有
`CONTACT_MAX_MSGS=60000` 硬上限兜底，命中时 `overview.truncated=true`。

## 安全模型

- **只绑 127.0.0.1**：不对外，没有认证层（本机工具）
- **无任意文件写**：导出产物永远落在 `.cache/gui_export/<task_id>/`，
  服务端不接受任何用户传路径
- **无任意文件读**：`/vendor/` 与 `/api/export/download` 都做
  `resolve()` + `startswith` 目录校验，`../` 与 `..%2f` 穿越均 404
- **无 XSS**：消息文本全部走 `v-text`/`{{ }}` 自动转义；
  搜索高亮用受控的 `v-html`（输入先转义再包 `<mark>`）；
  SVG 图表用 `v-for` 渲染元素，不拼 HTML 字符串
- 语料（`persona/`）与密钥（`.env`、`~/.mosu/`）均 gitignore，不进仓库

## 开发者验证清单

```bash
# 起服务
python cli/mosu.py gui --no-browser &

# 冒烟
curl --noproxy '*' http://127.0.0.1:8788/api/status
curl --noproxy '*' "http://127.0.0.1:8788/api/sessions?limit=5"
curl --noproxy '*' "http://127.0.0.1:8788/api/messages?session=<wxid>&limit=10"
curl --noproxy '*' "http://127.0.0.1:8788/api/stats/global"
curl --noproxy '*' -X POST http://127.0.0.1:8788/api/chat \
     -H 'Content-Type: application/json' -d '{"message":"在吗"}'

# 异步任务
TID=$(curl -s --noproxy '*' -X POST http://127.0.0.1:8788/api/stats/keywords \
     -H 'Content-Type: application/json' -d '{"top":20,"max_msgs":5000}' \
     | python -c "import json,sys;print(json.load(sys.stdin)['taskId'])")
curl --noproxy '*' "http://127.0.0.1:8788/api/task?id=$TID"

# 导出下载
curl -s --noproxy '*' -X POST http://127.0.0.1:8788/api/export \
     -H 'Content-Type: application/json' -d '{"session":"张三","format":"csv"}' -o out.csv
```

## 常驻

`mosu gui install` 优先注册 Windows 计划任务（登录即启动 + 崩溃 2 分钟间隔
自愈最多 5 次 + 无执行时长限制）。若本机策略禁止「登录时启动」的计划任务
（`Register-ScheduledTask` 对 AtLogOn 触发器报拒绝访问，企业安全策略常见），
自动降级为**启动文件夹 + VBS 守护脚本**（服务退出 5 秒内拉起）。

非 Windows 用 systemd / supervisor 守护 `python cli/mosu.py gui --no-browser`。
