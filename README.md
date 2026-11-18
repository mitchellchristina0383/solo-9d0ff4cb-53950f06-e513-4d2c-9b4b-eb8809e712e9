# Dependency Alert API

值班工程师面对一串服务故障告警时，需要先处理真正的**上游根因故障**，同时保留尚未恢复的
下游告警但予以抑制。本服务用 **FastAPI + SQLite（WAL）** 实现：

- 整版提交服务依赖图（`A` 依赖 `B`，则 `B` 是 `A` 的上游）；
- 带**每服务递增正整数序号**上报故障 / 恢复，支持幂等重试、冲突拒绝、迟到不回退；
- 按服务设置**上报有效秒数**（TTL）：超时未上报的服务进入"状态未知"，失联上游的旧故障
  不再压住下游告警；
- 查询时动态计算**需处理故障**与**被抑制故障**及其上游根因；
- 替换依赖图时按服务名保留状态与 TTL 配置，恢复后下游自动重新归类，无需重报；
- 所有写操作走序列化事务，并发上报 / 换版 / 配置 / 查询不会混合不同已提交状态。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接操作）
# 健康:  http://localhost:8000/health
```

SQLite 数据文件保存在命名卷 `alert-data`（容器内 `/data/alerts.db`）。

### 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ALERT_DB_PATH=./data/alerts.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/graph` | 整版替换依赖图。校验失败（环路 / 未知引用 / 重名 / 重边 / 自依赖）返回 `422` 并**保留旧版** |
| `GET` | `/graph` | 读取当前已提交图（含内部修订号 `revision`） |
| `POST` | `/reports` | 上报故障 / 恢复 |
| `PUT` | `/ttl/{service}` | 设置 / 调整该服务的上报有效秒数 |
| `GET` | `/ttl` | 列出全部 TTL 配置及其当前判定 |
| `GET` | `/alerts` | 查询告警分类（需处理 / 被抑制 / 根因 / 路径 / 过期） |
| `POST` | `/admin/reset` | 清空全部数据（测试 / 运维辅助） |
| `GET` | `/health` | 健康检查 |

### 提交依赖图

```json
POST /graph
{
  "version": "v1",
  "services": [
    {"name": "A", "dependencies": ["B", "C"]},
    {"name": "B", "dependencies": ["D"]},
    {"name": "C", "dependencies": ["D"]},
    {"name": "D", "dependencies": []}
  ]
}
```

`dependencies` 中列出的是**上游**服务；所有被引用服务必须在同一次提交中出现，
整图必须是 DAG（Kahn 算法检环）。每次成功提交 `revision` 递增。

### 上报故障 / 恢复

```json
POST /reports
{"service": "B", "seq": 7, "status": "fault"}
```

序号 `seq` 为**每个服务各自维护的严格递增正整数**，规则：

| 情况 | 结果 |
| --- | --- |
| `seq > last_seq` | 应用新状态 |
| `seq == last_seq` 且内容相同 | 幂等重试，`applied=false, duplicate=true`，状态不变 |
| `seq == last_seq` 但内容不同（fault/recover 互斥） | `409 conflict`，状态不变 |
| `seq < last_seq`（迟到上报） | `409 conflict`，**绝不回退状态** |
| 服务不在当前图中 | `404 unknown_service` |
| `seq <= 0` | `422`（请求体校验） |

序号允许有空洞（如 1 之后直接 3）。每次上报（含被拒绝的）都写入 `events` 审计表。

### 上报有效期（TTL）

```json
PUT /ttl/B
{"ttl_seconds": 60}
```

为某个服务设置上报有效秒数后，其**最后一次有效（被应用的）上报**在受理
`ttl_seconds` 秒后过期，服务进入**状态未知**。规则：

| 情况 | 结果 |
| --- | --- |
| 设置或缩短期限 | 按最后一次有效上报的**受理时刻**立即判定，已过期者当场进入状态未知（响应中 `expired=true`） |
| 调长期限 | **不能**让已过期上报复活；过期一经判定即被闩锁持久化 |
| 解除过期 | 只有**更高序号**的有效上报可以解除过期（同时刷新受理时刻） |
| 同号重试 / 被拒上报 | 不刷新受理时刻，不解除过期 |
| 未配置 TTL 的服务 | 沿用原有判定，永不过期 |
| 服务被移出当前图 | 配置保留但不参与判定，重新入图后恢复生效 |

过期判定所需的受理时刻与闩锁都持久化在 SQLite 中，**重启后仍能识别过期上报**。
`GET /ttl` 可查看每个配置服务的当前判定（图外服务 `expired=null`）。

### 查询告警

```json
GET /alerts
{
  "graph_revision": 1,
  "graph_version": "v1",
  "services": ["A", "B", "C", "D"],
  "actionable_faults": [{"service": "D", "last_seq": 1}],
  "suppressed_faults": [
    {
      "service": "B",
      "last_seq": 1,
      "root_causes": ["D"],
      "paths": [["B", "D"]]
    }
  ],
  "down_services": ["B", "D"],
  "unreported_services": ["C"],
  "healthy_services": [],
  "expired_services": [
    {
      "service": "Y",
      "last_status": "down",
      "last_seq": 4,
      "accepted_at": 1794991017.919417,
      "ttl_seconds": 60,
      "deadline": 1794991077.919417
    }
  ]
}
```

`expired_services` 即**状态未知**的服务：配置了 TTL 且最后一次有效上报已过期，
条目中给出其最后状态与序号。每个图节点必然且仅属于
`down_services` / `unreported_services` / `healthy_services` / `expired_services`
四类之一。

## 抑制规则（核心语义）

1. 只有当前仍处于故障（down）状态的服务才会出现在 `actionable_faults` 或
   `suppressed_faults` 中，两类互斥且并集恰为全部 down 服务。
2. **根因**：故障且没有任何故障上游的服务。根因永远是需处理故障。
3. 一个非根因故障被抑制，当且仅当存在一条到达某根因的依赖路径，且**路径上每个节点都
   处于故障中**。
4. 健康节点、**从未上报**的节点或**已过期**（状态未知）的节点会截断路径：其下游即使
   故障也不能经由该路径被抑制；若所有路径都被截断，该故障仍归入需处理。因此失联上游
   的旧故障一旦过期，仍在故障的下游会**立即成为待处理**。
5. 分类是查询时根据当前图 + 当前状态**动态计算**的。服务恢复、上报过期、依赖图换版后
   立即重新归类，不需要下游重新上报。

## 换版与状态保留

- 服务状态（up/down、最后序号、受理时刻、过期闩锁）独立于图存储，按服务名为主键。
  换版后同名服务的状态与序号历史原样保留；被移除服务的状态行不删除但不会出现在查询
  中，将来重新加入时恢复可见。
- TTL 配置同样按服务名存储，换版保留同名服务配置；被移出图的服务不参与过期判定，
  重新入图后配置恢复生效。
- 新图中首次出现的服务归入 `unreported_services`。

## 并发模型

- SQLite 开启 WAL；每个写事务以 `BEGIN IMMEDIATE` 开始，写者全局串行化，遇
  `SQLITE_BUSY` 短时重试。
- 读事务持有单一已提交快照，因此换版与查询并发时，每次 `/alerts` 看到的节点集、边与
  修订号必然属于同一个已提交版本，不会出现混合状态。
- 每个请求使用独立的线程级连接，请求结束即关闭。

## 自测

`tests/selftest.py` 仅依赖标准库，对运行中的服务做端到端校验：

```bash
python tests/selftest.py                      # 默认 http://127.0.0.1:8000
BASE_URL=http://localhost:8000 python tests/selftest.py
# 容器内：docker compose exec alert-api python tests/selftest.py
```

覆盖：图校验与原子替换、序号幂等 / 冲突 / 迟到语义、全故障路径抑制与健康 / 未上报 /
过期截断、恢复即时重分类、换版状态与配置保留，TTL 的立即判定 / 调长不复活 / 高序号
解除 / 重试不刷新，以及同序号并发重试、同序号异内容竞争、多序号乱序竞争、并发换版、
配置 + 上报 + 换版 + 查询交错等关键并发场景（共 94 项断言）。

## 目录结构

```
app/
  main.py            FastAPI 路由与异常映射
  database.py        SQLite 连接、schema、迁移、事务管理
  graph_service.py   图校验、整版替换、告警分类
  report_service.py  上报序号语义
  ttl_service.py     按服务上报有效期（TTL）配置与过期判定
  schemas.py         Pydantic 模型
  errors.py          领域错误
tests/selftest.py    端到端自测（标准库）
Dockerfile, docker-compose.yml, requirements.txt
```
