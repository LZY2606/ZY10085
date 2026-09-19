# diagforge

确定性的多文件编译器诊断最小化工具：给定一组能触发编译器崩溃 / 诊断漂移的
源文件、一条编译命令模板和一个期望谓词，diagforge 在隔离本地进程中反复运行
候选，用「删文件 → 删语法块 → 删 token」三层策略搜索仍满足谓词的最小样本。

## 安装与运行

```sh
python3 -m pip install -e '.[test]'
pytest -q
python3 -m diagforge --host 127.0.0.1 --port 5214
```

页面固定在 <http://127.0.0.1:5214>。点击「创建演示会话」会用内置的假编译器
`diagforge/demo/fakecc.py`（遇到 `TRIGGER` 即崩溃、遇到 `FLAKY` 产生不稳定
诊断）建立一个会话并自动开始缩减。

## 概念

- **会话 (session)**：原始文件集 + 命令模板 + 谓词 + 环境快照。会话 id 是
  输入内容的哈希，同样输入重复创建得到同一会话。
- **候选 (candidate)**：搜索树节点，记录父节点、产生它的变换、结果指纹。
  候选 id 是文件集内容的哈希。
- **谓词 (predicate)**：如 `{"exit_codes": [1], "stderr_contains": "ICE"}`。
  每个候选运行 K 次；K 次指纹完全一致且全部通过谓词才 *accepted*；指纹一致
  但不通过为 *rejected*；指纹不一致为 *unstable*（偶发结果进入不稳定分支，
  一次成功不能冒充稳定复现）。
- **环境快照 (snapshot)**：编译器二进制的 SHA-256 + 环境变量白名单。暂停后
  恢复时重新采集，指纹变化必须显式 `allow_new_branch` 开启新 epoch，旧结果
  不会被静默复用。
- **钉住 (pin)**：`keep_text`（某文件必须包含文本）、`keep_file`、
  `couple_files`（一组文件同留同删）。添加钉住只让违反约束的候选失效。
- **决定 (decision)**：人工操作记录操作者、理由和前后版本；撤销会追加一条
  `undo` 决定并还原效果，历史永不删除。

## 不变量

1. **确定性**：所有 id 与指纹都来自内容哈希；排序只用固定键；状态里不出现
   墙钟时间。同样输入无论遍历顺序、worker 返回顺序或运行时间如何，都得到
   相同的最终优先候选（`best = min(文件数, 字节数, id)`，是已接受集合的纯
   函数）。
2. **稳定性门槛**：候选只有在规定次数 K 中指纹完全一致且谓词成立才被接受。
3. **原子性**：每个引擎步进是一个 SQLite 事务；失败的批次回滚，不留下可见
   的部分结果（`runs`/`candidates`/`pending` 同生共死）。
4. **可暂停**：搜索树、待处理队列、层级状态全部持久化，任意步进后可暂停、
   恢复（快照未变时）继续。
5. **只失效受影响者**：新增钉住时仅把违反该约束的已评估候选标记为
   `invalidated`，其余候选与历史保持不变。
6. **可离线复核**：导出包含 `original/`、`minimal/`、`chain.json`（完整变换
   链 + 每步文件集哈希与指纹）和独立的 `replay.py`（仅标准库），逐步重放
   变换并重跑谓词验证。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sessions` | 会话列表（含尺寸、运行数、最早分歧、队列长度） |
| POST | `/api/sessions` | 创建会话 `{files, command, predicate, k_runs, timeout, env_whitelist}` |
| POST | `/api/demo` | 创建内置演示会话 |
| GET | `/api/sessions/{id}` | 会话详情与状态 |
| POST | `/api/sessions/{id}/control` | `start/pause/resume/step`，可带 `allow_new_branch` |
| GET | `/api/sessions/{id}/tree` | 搜索树（父节点、变换、指纹） |
| GET | `/api/sessions/{id}/runs/{cid}` | 某候选的 K 次运行记录 |
| POST | `/api/sessions/{id}/pins` | 添加钉住约束 |
| POST | `/api/sessions/{id}/decisions` | 记录人工决定 |
| POST | `/api/sessions/{id}/decisions/{seq}/undo` | 撤销（追加新事件） |
| POST | `/api/sessions/{id}/export` | 导出复核包 |

## 测试数据的含义

- `diagforge/demo/fakecc.py`：假编译器。输入含 `TRIGGER` 时向 stderr 打印
  internal compiler error 并退出 1；含 `FLAKY` 时诊断中混入 pid，模拟偶发
  崩溃（用于覆盖 unstable 分支）；否则退出 0。
- `diagforge/demo/src/`：演示用三文件样本，`main.c` 中的 `TRIGGER` 注释是
  崩溃根因，其余内容是应被裁掉的填充。
- `tests/`：覆盖缩减正确性、跨运行确定性、worker 顺序无关性、不稳定分支、
  钉住失效范围、快照变更新分支、决定/撤销的追加式历史、失败批次原子性、
  导出包离线 replay，以及 HTTP API 端到端流程。
