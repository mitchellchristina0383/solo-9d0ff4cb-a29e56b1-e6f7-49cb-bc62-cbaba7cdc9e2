# OCI Layer Merger

把两个连续的、**未压缩的** OCI 差异层（diff layer，tar 归档）合并成一个可复用的
差异层。合并层在任意下层（lower layer）上单独应用，与先应用第一个再应用第二个输入层
得到的路径、条目类型和文件内容完全一致。

服务同时提供 **在线组 / 批处理组 cpuset 重新绑核 API**（见下文“CPU 重新绑核”）。

## 支持的 tar 成员

| 成员 | 处理方式 |
| --- | --- |
| 普通文件 | 内容落盘到请求级临时目录，原样保留 mode/uid/gid/mtime 等元数据 |
| 目录 | 保留元数据；在"文件↔目录互换"、"删除后重建"时自动加 opaque 标记 |
| whiteout（`.wh.<name>`） | 删除同名子树；若在合并结果中仍需遮蔽下层则保留为 whiteout |
| opaque 目录标记（`.wh..wh..opq`） | 清除该目录下来自更低层的内容，同层条目始终保留 |

符号链接、硬链接、设备节点、FIFO 等其他成员类型一律拒绝（HTTP 400）。压缩 tar
（gzip/bzip2/xz/zstd，按魔数识别）、绝对路径、含 `..` 的路径、反斜杠/NUL 字符、
非零长度 whiteout、截断或损坏的 tar 同样拒绝。

## 为什么能在"任意下层"上等价

合并器在内存里维护一棵带 `origin`（来自第一层还是第二层）与 `opaque` 标记的树：

1. 顺序应用两个输入层，whiteout 删除子树，opaque 仅清除更低 `origin` 的节点；
2. 当一个目录建在"曾经是文件/被 whiteout 的目录"的位置时，强制打 opaque 标记，
   保证下层目录里可能存在的子项在任何基础层上都被隐藏；
3. 输出时，仍需遮蔽下层的路径输出 `.wh.` whiteout；opaque 目录输出
   `.wh..wh..opq`；其余按确定性顺序（目录、标记、文件）写出。

因此合并层携带的遮蔽信息不依赖于某个具体下层的内容。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs ，在 /merge 的表单里上传 lower 与 upper 两个 tar
```

`docker-compose.yaml` 以只读根文件系统、tmpfs `/tmp`、非 root 用户和
`no-new-privileges` 运行，内置 `/health` 健康检查。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

- `GET /health` — 存活探针，返回 `{"status":"ok"}`
- `GET /docs` — Swagger UI，可直接上传/下载，也可在页面上演示 cpuset 调整
- `POST /merge` — `multipart/form-data`，字段：
  - `lower`：第一个（较低的）未压缩 tar
  - `upper`：第二个（较高的）未压缩 tar

成功返回 `200 application/x-tar`（附件名 `merged-layer.tar`）；任一输入非法返回
`400 application/json`，不会产生可下载产物；超过 2 GiB 返回 `413`；缺字段返回
`422`。

## CPU 重新绑核（在线组 / 批处理组）

镜像层合成的两个工作组在本地文件上模拟为**同一父 cgroup 下的两个子组**
（`parent/online` 与 `parent/batch`，各自一个 `cpuset.cpus` 文件，父组另有
`cpuset.cpus.effective`）。父组至多拥有 8 个可用 CPU（默认 `0-7`）。

### 约束（每个步骤都必须成立）

1. 每次只能改一个组的 cpuset（另一个组保持运行、配置不动）；
2. 每一步之后两个组都仍满足各自的最低 CPU 数（默认 online ≥ 2、batch ≥ 1）；
3. 两个组任何时刻都不能共用同一个 CPU；
4. 可以临时借用父组的空闲 CPU 来腾出交换空间，借完再归还；
5. 不存在合法步骤序列能达到目标时，**保持原配置不动**，返回 `409` 且不写日志。

规划器（`app/cpuset.py` 的 `plan_moves`）用 BFS 在“单组一步”的状态空间里搜索最短
合法序列；每一个候选移动都会在写入前重新校验最低核数、父组边界和互不重叠。

### 持久化、中断与恢复

- 每次调整分配一个单调递增的**操作号**（`000001`、`000002` …），目标、计划和每一步
  已提交记录以 JSON 写入 `journal/ops/<操作号>.json`，写入采用 `临时文件 + rename +
  fsync` 的原子方式；
- cpuset 文件的写入同样是原子的：模拟的“写到一半被杀死”只会留下一个
  `*.interrupted` 临时文件，真正的 `cpuset.cpus` 仍是上一份完整内容；
- 写入中断后可通过 `GET /cpuset/allocation` 查看**实际分配**和未完成的操作号；
- 进程/容器重启后，凭操作号调用 resume：控制器重新读取磁盘上的实际分配，从当前状态
  重新规划到日志里记录的目标，沿用原操作号继续执行；
- 存在未完成操作期间，任何新的调整请求都被拒绝（`409`），直到该操作完成。

### cpuset 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /cpuset/allocation` | 从模拟 cgroup 文件读回两组的实际 CPU 分配、最低核数与未完成操作号 |
| `POST /cpuset/rebind` | 请求体 `{"online_cpus":[...],"batch_cpus":[...]}`；规划并执行一次调整，返回操作号、计划与已提交步骤 |
| `POST /cpuset/operations/{id}/resume` | 重启/中断后凭操作号从实际分配继续 |
| `GET /cpuset/operations/{id}` | 查询单个操作的状态与逐步提交记录 |
| `GET /cpuset/operations` | 列出全部操作（按操作号排序） |

错误码：目标语义非法（越界、低于最低核数、两组重叠、重复编号、非整数）→ `400`；
请求体缺字段/类型错误 → `422`；无合法计划或已有未完成操作 → `409`；操作号不存在 →
`404`；cpuset 写入被中断 → `500`（随后用 resume 恢复）。

### 拓扑与故障注入（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CPSET_STATE_DIR` | `/var/lib/cpuset-controller` | 模拟 cgroup 树与操作日志目录（compose 中为持久卷） |
| `CPSET_PARENT_CPUS` | `0-7` | 父组可用 CPU（至多 8 个） |
| `CPSET_ONLINE_MIN_CPUS` / `CPSET_BATCH_MIN_CPUS` | `2` / `1` | 两组各自的最低 CPU 数 |
| `CPSET_ONLINE_INITIAL_CPUS` / `CPSET_BATCH_INITIAL_CPUS` | `0-2` / `3` | 全新状态目录的初始分配（已有目录则以磁盘实际分配为准） |
| `CPSET_FAIL_ON_WRITE` | 未设置 | 设为正整数 N 时，每次调整的第 N 次 cpuset 写入模拟中断（演示用） |

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
```

测试套件包含：

- 一个**独立实现的参考叠加器**（`tests/helpers.py`，节点树结构与生产代码不同），
  在两个刻意构造的不同下层 `BASE_A` / `BASE_B` 上逐场景比较
  `base+lower+upper` 与 `base+merged` 的完整快照；
- whiteout 跨层保留、目录删除重建、文件/目录双向互换、opaque 同层语义、
  根 opaque、嵌套 whiteout 等场景；
- 压缩输入、截断 tar、危险路径、符号链接、非空 whiteout、垃圾数据等无效输入；
- 基于 FastAPI TestClient 的 HTTP 集成测试（状态码、Content-Type、下载内容）；
- cpuset 规划器单测：最短合法序列、借核互换、满载无空闲时不可达、每步单组变更、
  最低核数与互不重叠、60 组随机拓扑的逐步合法性；
- cpuset 持久化测试：写入中断只留临时文件且实际分配不变、重启后凭操作号从实际分配
  恢复（首/中/末写入分别中断）、未完成期间拒绝其他调整、操作号单调递增；
- 并发回归测试：同进程多线程原子写互不破坏、空目录上并行构建控制器不产生竞态。

针对已部署服务的端到端脚本（仅用标准库，可用 `python -I` 运行）：

```bash
# 镜像层合并自测
python -I scripts/selftest.py --base-url http://127.0.0.1:8000
# 追加 cpuset 借核互换、非法目标、互斥与（故障注入下的）中断恢复自测
python -I scripts/selftest.py --base-url http://127.0.0.1:8000 --with-cpuset
```

### 手动演示借核互换与写入中断

```bash
# 1) 正常启动，在 /docs 的 POST /cpuset/rebind 里试：
#    {"online_cpus":[3,4],"batch_cpus":[0,1,2]}
docker compose up --build

# 2) 中断恢复演示：把 docker-compose.yaml 里 CPSET_FAIL_ON_WRITE 设为 "1" 后重启，
#    发起一次调整会得到 500，GET /cpuset/allocation 显示 in_progress_operation；
#    再把该变量清空重启（持久卷保留日志），调用
#    POST /cpuset/operations/000001/resume 即可从实际分配继续到完成。
```

## 已知限制（OCI 单差异层模型的固有边界）

合并器只依据两个输入 tar 的可见信息决定 opaque 标记。考虑这种情形：输入层
只包含一个普通目录成员 `d/`（没有 whiteout、没有同层文件互换），而某个未知下层
在同一路径 `d` 上恰好是一个**普通文件**。由于输入层没有任何信号表明发生过类型
互换，合并层无法在不假设下层内容的前提下判断是否需要 opaque——这是单个 OCI
差异层表达能力的固有限制，任何只看这两个 tar 的合并器都面临同样的不可判定性。

所有**在输入层内可观察**的遮蔽需求都被完整处理并测试覆盖：

- 输入层用 whiteout 删除目录后重建 → 重建目录带 `.wh..wh..opq`；
- 输入层内文件↔目录互换 → 新目录带 opaque，隐藏下层目录可能存在的子项；
- 输入层 opaque 标记、嵌套 whiteout、根 opaque → 原样保留并在两层间正确合成。

其他限制：仅接受未压缩 tar（按魔数拒绝 gzip/bzip2/xz/zstd）；不支持符号链接、
硬链接、设备节点、FIFO 等非常规成员；单层上传上限 2 GiB。

## 目录结构

```
app/
  merger.py   # 核心合并逻辑（无 Web 依赖，可单独复用）
  cpuset.py   # cpuset 规划器、cgroup 文件模拟、操作日志与崩溃恢复
  main.py     # FastAPI 路由、上传落盘、错误处理
tests/        # pytest 单元与集成测试
scripts/      # 针对运行中服务的独立自测脚本
Dockerfile
docker-compose.yaml
requirements.txt
```

`docker-compose.yaml` 以只读根文件系统、tmpfs `/tmp`、非 root 用户和
`no-new-privileges` 运行，内置 `/health` 健康检查；cpuset 的状态目录
`/var/lib/cpuset-controller` 挂载为命名卷 `cpuset-state`，容器重启后日志与模拟
cgroup 文件仍然保留，恢复流程不依赖内存状态。
