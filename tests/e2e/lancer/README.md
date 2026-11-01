# 对标 Lancer 的端到端验证脚本

这组脚本验证的是**对标 Lancer 迭代 1–4 交付的能力**，
和 `../run_e2e.py`（覆盖全部 GUI 页面）是两回事：

- `run_e2e.py`：**广度**——把 10 个页面都点一遍，抓白屏和崩溃
- 这里：**深度**——每轮迭代的核心能力单独拉出来，用真模型 + 真数据验

## 前置条件

```bash
# 1. GUI 服务必须在跑（会连真实模型、真实微信库）
python cli/mosu.py gui --port 8788 --no-browser

# 2. 依赖：playwright + 真实 LLM 端点（.env 里配）
```

## 各脚本验什么

| 脚本 | 对应迭代 | 证据级别 | 核心断言 |
|---|---|---|---|
| `e2e_iter1_l5.py` | 迭代 1 多代理并行 | **L5** | 真机触发委派、3 条泳道、结论互相独立、**并行收益**（墙钟 < 串行耗时和） |
| `e2e_iter2.py` | 迭代 2 SSE + 历史 | **L5** | SSE 首事件延迟、落盘与流**逐字一致**、真机 2s 见轨迹、历史面板可展开 |
| `e2e_iter3.py` | 迭代 3 派活通道 | **L5** | 真 socket 真模型、忙时 BUSY / queue 排队 / steer 递进、鉴权拒绝、**CLI 退出码非 0** |
| `e2e_iter4.py` | 迭代 4 分支树 | **L5** | 3 泳道 3 根连线、颜色继承、**锚点缺失不乱飞**、console 0 错误 |
| `e2e_iter6.py` | 迭代 6 模型目录 | **L5** | **模型名优先于 provider** 归类、三态真实存在、切换模型下游真的生效、旧配置继续可用、前端家族条真渲染 |
| `e2e_iter7.py` | 迭代 7 阶段契约 | **L5** | 契约随答案返回、status code 带 okToProceed、每阶段一个 gate、机械摘要无叙述、forbidden 逐条判定、落盘带契约、前端阶段带三态可区分 |

## 跑法

```bash
python tests/e2e/lancer/e2e_iter1_l5.py
python tests/e2e/lancer/e2e_iter2.py
python tests/e2e/lancer/e2e_iter3.py
python tests/e2e/lancer/e2e_iter4.py
python tests/e2e/lancer/e2e_iter6.py
python tests/e2e/lancer/e2e_iter7.py
```

退出码：`0` 全通过；`1` 有断言失败。
截图落在 `.cache/audit/v2/`。

## 为什么这些脚本要入库

原先全堆在 `.cache/audit/`（gitignored），后果是：

1. **换个人接手就没法复现验证**——脚本在哪、怎么跑、断言什么，全靠对话记录
2. **改了前端不知道该跑哪个**——没有索引
3. **验证记录没法进仓库**——`docs/verification/*.json` 里的 `evidence`
   指向本地路径，读者无法核对

现在脚本入库 + `docs/verification/` 有结构化报告 + `docs/verification.md`
有分级规则，三者对得上。

## 相关的轻量替代

上面这些都要真模型（一次 30s+）。如果只是想快速回归前端行为，
用**离线**的那套：

```bash
python -m pytest tests/test_branch_tree_render.py -p no:typeguard -q
```

固定数据 + 真浏览器，14 秒跑完，不依赖 GUI 服务。

## 写新脚本的三条规矩

1. **断言前先确认前置条件成立**。
   迭代 4 的教训：「隐藏节点后连线全部消失」在连线数为 0 时会**空跑通过**，
   把真 bug 完整盖住。要写成 `assert before > 0`。
2. **退出码必须非 0**。脚本里的失败要能被 CI 看见——
   迭代 3 的 CLI 退出码恒为 0 就是这么被藏了很久。
3. **改了后端 ≠ 服务在跑新代码**。跑之前核对
   `进程 CreationDate > 文件 LastWriteTime`。
