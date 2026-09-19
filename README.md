# diagforge

面向编译器团队的多文件诊断样本最小化工具：在隔离本地进程中反复运行候选，
只接受**稳定**满足期望谓词的缩减，并把整个搜索过程保存为可暂停、可恢复、
可离线复核的搜索树。

## 安装与运行

```sh
python3 -m pip install -e '.[test]'
pytest -q
python3 -m diagforge --host 127.0.0.1 --port 5214
```

页面固定在 <http://127.0.0.1:5214>。首次启动且数据库为空时会播种一个演示
会话（`examples/sample` + `examples/fakecc.py`），后台 worker 自动开始缩减。

其他命令：

```sh
python3 -m diagforge --db state.db          # 持久化状态，可暂停/恢复
python3 -m diagforge verify <导出目录>       # 离线复核导出包
python3 <导出目录>/verify.py <导出目录>      # 不依赖 diagforge 的独立复核
```

## 不变量

1. **确定性**：所有 id（会话、分支、候选、事件）都由内容哈希导出，不依赖
   遍历顺序、worker 数量或当前时间。相同输入 + 相同 `RULES_VERSION` 必然
   得到相同的最终优先候选（按 `(size, id)` 排序取最小）。
2. **稳定性门槛**：候选只有在规定次数（谓词中的 `runs`）中每次都满足谓词
   才被接受；结果不一致的候选归入 `unstable` 分支，一次偶然成功不能冒充
   稳定。
3. **搜索树可追溯**：每个候选记录父节点、产生它的变换和全部运行的结果
   指纹（退出码、stdout、stderr、超时、生成物摘要的哈希）。
4. **钉住约束**：文本钉（`span`）要求指定文件始终包含指定文本；共同保留
   （`co_keep`）要求一组文件同留同去。新增约束只使违反它的
   `pending/running` 候选失效，历史与未受影响候选不变。
5. **实验快照**：会话创建与恢复时记录编译器二进制哈希、`--version` 输出
   与环境白名单值。恢复时指纹变化必须开新分支，不混用不同工具链的结果。
6. **人工决定可审计**：接受/拒绝记录操作者、理由和前后指纹；撤销是追加
   一个补偿事件，历史永不删除。
7. **批次原子性**：多行写入都在事务中完成，失败的批次不留下任何可见的
   部分结果（导出包同样先在临时目录构建再原子改名）。
8. **可复算**：导出包含原始样本、最小样本、完整变换链与逐步运行指纹，
   `verify.py` 可离线重放每一步并重新检查谓词。

## 测试数据的含义

- `examples/fakecc.py`：假编译器。任何输入文件包含 token `trigger` 时以
  退出码 42 崩溃并在 stderr 打印 `internal compiler error`，否则退出 0。
- `examples/sample/`：三文件 C 小项目，`trigger` 埋在 `util.c` 的
  `stage_lower` 函数里。期望的最小化方向是删掉无关文件并把 `util.c`
  缩减到仍含 `trigger` 的小片段。
- `tests/conftest.py` 中的 `make_runner`：进程内假运行器，语义与
  `fakecc.py` 相同，但可注入“首次运行翻转结果”的不稳定候选，用于验证
  不稳定分支不会被接受。

## API 摘要

- `GET  /api/state`：尺寸、运行次数、最早分歧、队列、计数、事件。
- `GET  /api/candidate?id=`：候选内容与运行记录。
- `POST /api/session`：上传文件集合 + 命令模板 + 谓词，创建会话。
- `POST /api/pin`：新增文本钉或共同保留约束（仅使受影响候选失效）。
- `POST /api/decision` / `POST /api/undo`：人工决定与撤销（追加事件）。
- `POST /api/resume`：检测快照指纹，必要时开新分支。
- `POST /api/export`：导出可离线复核的包。
