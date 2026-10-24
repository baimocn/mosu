# 试点数据追踪与 Day 3 判定

每天花 2 分钟填 `tracker.csv`。Day 3 晚上按下面的规则做 go/no-go——
**规则先行，防止到时候拍脑袋**。

## 填法

| 列 | 哪来 |
|---|---|
| date | 当天日期 |
| channel | smithery / agenticmarket / devto / zhihu / v2ex / xianyu / referral |
| installs | 平台后台的安装/调用数；没有后台就填 GitHub clone 数（`gh repo view --json` 或网页 Insights） |
| pro_inquiries | 闲鱼 IM + issue + 邮件里问 Pro 的人数 |
| pro_orders | 实际付款笔数 |
| post_views | 帖文平台阅读量 |
| notes | 一句话：今天发生了什么（骂声也算数据） |

## 判定规则（Day 3 晚执行，约 20 分钟）

按优先级从上到下匹配，**命中即停**：

1. **Pro 询单 ≥ 5** → 需求信号强：提前做 license key 机制（原计划 Day 7+ 提前），
   同时把「远程帮装」排期表建起来（防支持黑洞）。
2. **安装 ≥ 10** → 分发成立：加投第二个入口位（微信分身物料），闲鱼商品加库存描述。
3. **0 < 安装 < 10 且帖文阅读 > 200** → 出口问题：安装步骤卡住。头号嫌疑是
   Python 环境（`pip install -r requirements.txt` 对小白就是墙）。改物料：
   README 首屏加「没有 Python？等 EXE 版」+ 录一个 90 秒安装 GIF。
   **不动产品本身。**
4. **安装 = 0 且帖文阅读 < 50** → 渠道问题：换渠道（V2EX/知乎重发），物料不动。
5. **安装 = 0 且阅读 > 200** → 钩子问题：首屏没说清「AI 能读微信数据」的价值，
   重写首屏一句话（README-discover.md 的修改 1 再加锋利）。

## 三条纪律

- **7 天零安装不改产品，改物料**——出口问题用出口解决。
- **数据连续记 7 天**，Day 3 只是中检；最终复盘在 Day 7。
- **基准线**（案例证据，不是承诺）：21st.dev 靠 MCP 市场发现做到 6 周 $10K MRR；
  个人开发者 26 个 MCP 服务器的量级是周 92 次 npm 下载。我们 7 天内的现实目标是
  **≥50 installs 或 ≥3 Pro 询单**——达到即验证，达不到按规则 3/4/5 分流。
