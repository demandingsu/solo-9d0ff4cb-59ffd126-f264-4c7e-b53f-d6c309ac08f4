# 农业遥感同观测日选景 API

为一批地块选取**同一 UTC 观测日**的遥感影像，供长势对比。基于 **FastAPI + SQLite**，
通过 Docker Compose 一键启动，自带 `/docs` 交互式 API 文档。

## 选景规则

1. 上传地块矩形范围与候选场景（场景号、UTC 观测日、覆盖矩形、云量、来源行号）。
2. 某个观测日只有在**同一天的场景能覆盖全部地块**，且每块地块所选场景
   **云量 ≤ 云量阈值**（含边界）时，才记为可行日期。
   - 覆盖判定：场景覆盖矩形完全包含地块矩形（边界相接算覆盖）。
   - 同一天某地块有多个覆盖场景时，取云量最低者。
3. 在所有可行日期中，按**各地块所选场景的云量总和**最低者入选；
   总和相同时取观测日最早的日期。
4. 无可行日期时返回 `infeasible`，并逐地块、逐日期列出原因：
   - `uncovered`：该日期没有场景覆盖此地块；
   - `cloud_exceeded`：覆盖此地块的最低云量场景仍超过阈值。
5. 结果保留每个选用场景的**场景号与原始来源行号**。

## 数据可靠性设计

- **失败上传不改变现役数据**：每次上传在单事务内写入批次、地块、场景并退役旧批次，
  任一步失败整体回滚；请求体校验失败（422）根本不会触碰数据库。
- **清单更新后旧交付仍可读**：采用 append-only 版本化模型。重新上传只新增批次
  （旧批次置 `superseded`，数据不删除），每次选景生成不可变交付快照，
  可永久通过 `GET /deliveries/{id}` 读取当时选用的场景与来源行号。
- SQLite 数据库文件位于持久化卷 `scene-data`，容器重建后数据保留。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开
#   API 文档（Swagger UI）: http://localhost:8000/docs
#   OpenAPI 规范:          http://localhost:8000/openapi.json
```

无 Docker 时本地运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/uploads` | 上传一批地块与候选场景（事务化，失败回滚） |
| POST | `/deliveries` | 对现役批次执行选景，生成不可变交付快照 |
| GET | `/deliveries` | 全部交付摘要（新的在前） |
| GET | `/deliveries/latest` | 现役批次的最新交付 |
| GET | `/deliveries/{id}` | 按 ID 读取任意历史交付（含旧批次） |
| GET | `/batches` | 全部上传批次（含 superseded） |
| GET | `/batches/{id}/parcels` | 批次内地块明细 |
| GET | `/batches/{id}/scenes` | 批次内候选场景明细（含来源行号） |
| POST | `/same-scene-assertions` | 按两条场景的批次号+来源行号登记/撤销同景认定（事务化） |
| GET | `/same-scene-assertions` | 同景认定事件流（append-only，含已撤销事件） |
| GET | `/scene-groups` | 全部当前影像组及成员 |
| GET | `/scene-groups/{groupNo}` | 按组号查看当前组成员 |
| GET | `/scene-groups/by-old-no/{oldGroupNo}` | 按旧组号回溯原成员当前去向 |
| GET | `/health` | 健康检查 |

## 同景认定与影像组

数仓按影像组号对场景去重；发现误拆组后，还要能用**旧组号**找回当时那批场景的去向。
本服务在选景之外提供同景认定能力：

### 认定规则

1. **定位方式**：每条场景用「批次号 + 来源行号」唯一定位，请求体给出两条场景。
2. **同观测日限制**：只可认定**同一 UTC 观测日**的两条场景（跨批次同日允许，异日返回 409）。
3. **可传递**：认定关系是无向边，连通分量即同一个影像组——A≡B、B≡C 则 A/B/C 同组。
4. **撤销即拆边**：撤销某条认定只删除这一条边；撤销后不再与任何场景相连的参与场景
   **单独成组**（仍会获得组号）。
5. **组号发放**：
   - 变更后分量成员集合与变更前**完全相同**（重复登记、撤销后仍经其他边相连）→
     **沿用现号**，响应中 `reused=true`；
   - 成员发生变化（合并、拆开、孤立成组）→ **发放新组号**，`reused=false`；
   - 响应的 `group_nos` 返回本次涉及场景当前所在的全部组号。
6. **旧组号回溯**：每次发号都写不可变成员快照。`GET /scene-groups/by-old-no/{号}`
   返回旧组的原成员，以及**每个仍含有原成员的现组（各列一次）**，并列出落在其中的原成员。
7. **事务原子性**：认定关系与组号在同一事务内生效，任一步失败整体回滚，
   失败时查询仍返回改动前的结果（与上传同等级别的可靠性保证）。

### 认定请求示例

```bash
# 登记：批次 3 的来源行 1 与行 2 为同景
curl -X POST http://localhost:8000/same-scene-assertions \
  -H "Content-Type: application/json" \
  -d '{"action":"assert",
       "first_scene":{"batch_id":3,"source_line":1},
       "second_scene":{"batch_id":3,"source_line":2}}'

# 撤销同一条认定（action 改为 revoke）
curl -X POST http://localhost:8000/same-scene-assertions \
  -H "Content-Type: application/json" \
  -d '{"action":"revoke",
       "first_scene":{"batch_id":3,"source_line":1},
       "second_scene":{"batch_id":3,"source_line":2}}'

# 旧组号回溯（数仓找回原批次场景去向）
curl http://localhost:8000/scene-groups/by-old-no/G2
```

### 在 /docs 试跑「拆组再合并」与「撤销后仍相连」

1. `POST /uploads` 上传同一 UTC 观测日的 4 条场景 A/B/C/D（来源行号 1~4）。
2. `POST /same-scene-assertions` 依次登记 A≡B、B≡C：传递性使 A/B/C 合并，
   两次分别得到新组号 G1、G2；再登记 A≡C（冗余边）→ 成员没变，沿用 G2。
3. 撤销 A≡B：A 仍经 A≡C 与 B/C 相连 → 整组成员不变，**沿用 G2**（撤销后仍相连）。
4. 再撤销 A≡C：分量真正拆开，A 单独成组、B/C 成组，两个组**都发新号**（拆组）。
5. 重新登记 A≡B：单组与二元组合并，再发新号（再合并）。
6. `GET /scene-groups/by-old-no/G2`：用旧组号找回 A/B/C 现在的组与成员。
`GET /same-scene-assertions` 可查看完整事件流（含撤销记录）。

## 上传请求示例

见 [`examples/sample_upload.json`](examples/sample_upload.json)：

```bash
curl -X POST http://localhost:8000/uploads \
  -H "Content-Type: application/json" \
  -d @examples/sample_upload.json

curl -X POST http://localhost:8000/deliveries  # 选景
curl http://localhost:8000/deliveries/latest   # 查看结果
```

字段约束：经纬度矩形 `min_lon < max_lon`、`min_lat < max_lat`；
云量与阈值范围 0~100；`observation_date` 为 `YYYY-MM-DD`（UTC 观测日）；
地块编号与来源行号在同一批次内不可重复。

## 自测

```bash
pip install httpx pytest
pytest -v                      # 16 个用例：换日/缺覆盖/失败上传/回滚/版本快照/
                               # 同景认定传递/拆组合并/组号沿用/旧组回溯/事务回滚等

# 对已运行的服务（含容器）做端到端冒烟（仅标准库）：
python scripts/smoke_test.py http://localhost:8000
```

## 目录结构

```
app/
  config.py      # 数据库路径配置（SCENE_DB_PATH 环境变量）
  database.py    # SQLite 连接、建表、事务上下文
  schemas.py     # Pydantic 请求/响应模型与输入校验
  selection.py   # 核心选景算法（覆盖判定、可行日期、最低云量总和、原因生成）
  groups.py      # 同景认定：有效边/连通分量、组号沿用与发放、旧组号回溯
  main.py        # FastAPI 路由
tests/           # pytest 自测（test_api.py 选景，test_groups.py 同景认定）
scripts/         # 端到端冒烟脚本
examples/        # 上传请求示例
Dockerfile, docker-compose.yml
```
