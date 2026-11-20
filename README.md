# OCI Layer Merger

把两个连续的、**未压缩的** OCI 差异层（diff layer，tar 归档）合并成一个可复用的
差异层。合并层在任意下层（lower layer）上单独应用，与先应用第一个再应用第二个输入层
得到的路径、条目类型和文件内容完全一致。

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
- `GET /docs` — Swagger UI，可直接上传/下载
- `POST /merge` — `multipart/form-data`，字段：
  - `lower`：第一个（较低的）未压缩 tar
  - `upper`：第二个（较高的）未压缩 tar

成功返回 `200 application/x-tar`（附件名 `merged-layer.tar`）；任一输入非法返回
`400 application/json`，不会产生可下载产物；超过 2 GiB 返回 `413`；缺字段返回
`422`。

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
- 基于 FastAPI TestClient 的 HTTP 集成测试（状态码、Content-Type、下载内容）。

针对已部署服务的端到端脚本（仅用标准库，可用 `python -I` 运行）：

```bash
python -I scripts/selftest.py --base-url http://127.0.0.1:8000
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
  main.py     # FastAPI 路由、上传落盘、错误处理
tests/        # pytest 单元与集成测试
scripts/      # 针对运行中服务的独立自测脚本
Dockerfile
docker-compose.yaml
requirements.txt
```
