# 参与墨溯开发

感谢愿意搭把手的开发者。墨溯是**零运行时依赖优先**的纯 Python 工具
（标准库 + 4 个 PyPI 包），所有改动请守住这条底线。

## 环境

- Python >= 3.10（开发与 CI 覆盖 3.10 ~ 3.13）
- `pip install -r requirements.txt`（pycryptodome / zstandard 必装；
  jieba / Pillow 可选，缺失时功能自动降级）
- 跑测试：`python -m pytest tests/ -q`

> 注：若本机 Python 3.14 + pytest 环境装了 typeguard 插件，
> 需加 `-p no:typeguard`（typeguard 与 3.14 的 ast.Str 不兼容，纯环境问题）。

## 代码约定

1. **零新增运行时依赖**。新功能先用标准库实现；确需依赖先开 issue 讨论。
2. **中文注释**，解释「为什么」而不是复述「是什么」。
3. **异常转换**：HTTP 层所有错误转 JSON `{"error": ...}`，参数错 400、
   不存在 404、其余 500，不允许 500 裸奔；注意 `SystemExit` 不是
   `Exception` 子类，core 层抛它时必须显式捕获。
4. **数据层不加锁**：`Dataset` 的连接是共享的，多线程消费者
   （GUI/MCP）在外层持锁串行化。
5. **隐私铁律**：真实语料（`persona/`）、密钥（`.env`、`~/.mosu/`）、
   导出物（`.cache/`、`export/`）永不入库；提交前跑隐私扫描：
   ```bash
   git ls-files | grep -iE "persona|corpus|\.env|\.cache|export/"   # 必须为空
   ```
6. **不碰微信进程**：不注入、不 hook、不内存扫描、不写微信的任何文件。
7. 改核心行为（解密/解析）必须带测试；测试全离线 stub，不碰真实数据。

## 提交信息

跟随仓库现有风格：`type(scope): 中文描述`，type 取
`feat / fix / docs / refactor / test / chore`。body 说明动机与验证方式
（curl / 截图 / pytest 结果），一个 commit 做一件事。

## PR 流程

1. Fork → 特性分支（`feat/xxx` / `fix/xxx`）
2. 本地 `python -m pytest tests/ -q` 全绿
3. PR 描述写清：改动动机、验证方式、隐私自查勾选
4. 涉及 GUI 的改动附 Edge/Chrome headless 截图

## 已知坑（改这些地方前先读）

- **SQLite 跨线程**：`Dataset.conn()` 用 `check_same_thread=False`，
  但同一时刻只能一个线程用——多线程服务必须自己持锁
- **WAL 合并不了**：微信 4.1 的 WAL 帧 salt 与主库不一致，别尝试实时合并
- **Analytics 抛 SystemExit**：找不到会话时抛的是 `SystemExit` 不是
  `ValueError`（`core/analytics.py`），HTTP 层要显式转 4xx
- **图片 V2 密钥**：账号级 AES key 不常驻内存，需先在微信里打开过一张图
