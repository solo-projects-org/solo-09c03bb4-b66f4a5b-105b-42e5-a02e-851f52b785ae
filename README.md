# 医院计数矩阵遮盖核查与月报同步 API

本服务包含两个面向市级数仓的能力：

1. **遮盖核查**：发布分年龄组（列）、乡镇（行）的病例计数前，检查 `null`
   遮盖格能否被**公开的行/列合计精确反推**。对每个隐藏格，在所有合计同时
   成立的前提下返回：
   - 可行最小值 `min`、最大值 `max`（隐藏值可取任意非负整数）；
   - 分别达到两界的完整可行矩阵 `minimizer_matrix` / `maximizer_matrix`；
   - `min == max` 时标记 `recoverable: true`，表示该格可被唯一反推，
     工程师应调整遮盖位置后重新核查；
   - 约束无解时返回 HTTP 409 与结构化冲突说明，**不输出任何区间**。
2. **月报同步**：院方按统计月提交已发布的整表（更正即换整张表）或撤刊
   通知；每笔变更写入 SQLite 并分配全局递增序号，下游数仓带游标分页拉取，
   轮次冻结保证同游标重试与服务重启后仍能按原轮次续页。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用与路由（含 Swagger 示例）
│   ├── schemas.py     # Pydantic 请求/响应模型、内置示例
│   ├── solver.py      # Dinic 最大流可行性判定 + HiGHS 线性规划求界
│   ├── db.py          # SQLite 变更流与同步轮次持久层
│   └── sync.py        # 轮次冻结、每月折叠、游标分页与令牌
├── tests/             # pytest 测试（含暴力枚举交叉验证）
├── Dockerfile
├── docker-compose.yml # 挂载命名卷 sync-data 持久化 /data/monthly_reports.db
├── requirements.txt
└── requirements-dev.txt
```

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs> （可直接选用内置示例提交）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`。

## 请求与响应

`POST /api/v1/suppression-check`

请求体（矩阵最多 12×12；`null` 为遮盖格，其余为固定非负整数）：

```json
{
  "matrix": [
    [5, null, null],
    [null, 3, null],
    [null, null, 2]
  ],
  "row_totals": [12, 10, 8],
  "col_totals": [9, 9, 12]
}
```

可行响应（HTTP 200，节选）：

```json
{
  "status": "feasible",
  "grand_total": 30,
  "hidden_count": 6,
  "recoverable_count": 0,
  "cells": [
    {"row": 0, "col": 1, "min": 0, "max": 4, "recoverable": false}
  ],
  "minimizer_matrix": [[5, 0, 7], [4, 3, 3], [0, 6, 2]],
  "maximizer_matrix": [[5, 4, 3], [0, 3, 7], [4, 2, 2]]
}
```

无解响应（HTTP 409，不含任何区间信息）：

```json
{
  "status": "conflict",
  "reasons": [
    "不存在满足全部公开合计的非负整数矩阵：最大流可行性检验未通过",
    "行子集 [0] 的隐藏格需求合计为 6，但这些行的隐藏格只能进入列 [0]，其可承接供给仅为 2（Hall 条件被破坏：需求 > 邻接供给）",
    "最大流 6 < 隐藏格所需总流量 10"
  ],
  "witness": {
    "max_flow": 6,
    "required_flow": 10,
    "row_subset": [0],
    "neighbor_cols": [0],
    "subset_demand": 6,
    "neighbor_supply": 2
  }
}
```

状态码约定：

| 场景 | 状态码 |
| --- | --- |
| 存在可行矩阵 | 200，返回区间与两张完整矩阵 |
| 合计约束无解（总量不符 / 固定格超限 / Hall 条件破坏） | 409，返回冲突原因与见证 |
| 请求结构或类型错误（超 12×12、负数、布尔、长度不符） | 422，Pydantic 校验信息 |

## 月报同步

### 数据模型（SQLite，默认 `data/monthly_reports.db`，容器内 `/data/monthly_reports.db`）

- `report_changes`：院方每提交一笔追加一行，`seq` 为
  `INTEGER PRIMARY KEY AUTOINCREMENT`，**全局递增、不复用**；字段含统计月、
  变更类型（`full_table` / `retraction`）、整表 JSON、入库时间。
- `sync_rounds`：下游每个游标首次发起同步时冻结一行
  `(after_seq, cutoff_seq, created_at)`，`cutoff_seq` 取查询开始时已提交的
  最大序号。轮次落库后，同游标的任何重试（含服务重启）都按同一截止序号
  续页。

### `POST /api/v1/monthly-reports` — 院方提交变更

- 发布/更正：`change_type=full_table`，`table` 沿用核查接口同一套遮盖矩阵
  与行/列合计格式（最多 12×12，`null` 为遮盖格）；同一统计月再次提交即
  更正，下游以最后一笔为准换整张表。
- 撤刊：`change_type=retraction` 且不得携带 `table`，下游收到后删除该月
  本地副本。

```json
{
  "stat_month": "2026-09",
  "change_type": "full_table",
  "table": {
    "matrix": [[5, null, null], [null, 3, null], [null, null, 2]],
    "row_totals": [12, 10, 8],
    "col_totals": [9, 9, 12]
  }
}
```

成功返回 HTTP 201 与分配到的序号：

```json
{"seq": 1, "stat_month": "2026-09", "change_type": "full_table",
 "committed_at": "2026-11-11T13:27:08Z"}
```

### `GET /api/v1/monthly-reports/sync` — 下游增量拉取

查询参数：

| 参数 | 说明 |
| --- | --- |
| `after_seq` | 上次收完的序号，首次为 `0`；只取严格大于该序号的变更 |
| `page_size` | 每页条数，1..100 |
| `page_token` | 上一页返回的 `next_page_token`，取首页留空 |

语义保证：

1. **轮次冻结**：首页请求开始时以已提交最大序号作为本轮 `cutoff_seq` 并
   落库；窗口为 `(after_seq, cutoff_seq]`。轮次进行中院方再提交的变更
   （包括更正）在本轮不可见，下一轮才会拉到。
2. **每月折叠**：窗口内同一统计月只保留最后一笔（更正覆盖前表、撤刊
   覆盖发布），再按 `seq` 升序分页。
3. **重试幂等**：同一游标、同一令牌重试返回同一页；服务重启后轮次与
   令牌仍有效（状态全在 SQLite）。
4. **续轮游标**：每页都带 `cutoff_seq`，末页 `has_more=false`；下游以
   该值作为下一轮的 `after_seq`。
5. 游标之后暂无变更时返回空页（`cutoff_seq == after_seq`），不冻结
   轮次，稍后用同一游标重试即可。

响应示例：

```json
{
  "after_seq": 0,
  "cutoff_seq": 2,
  "page_size": 1,
  "offset": 0,
  "has_more": true,
  "next_page_token": "eyJhZnRlcl9zZXEiOjAsImN1dG9mZl9zZXEiOjIsIm9mZnNldCI6MX0=",
  "changes": [
    {
      "seq": 1,
      "stat_month": "2026-09",
      "change_type": "full_table",
      "table": {"matrix": [[5, null, null], [null, 3, null], [null, null, 2]],
                "row_totals": [12, 10, 8], "col_totals": [9, 9, 12]},
      "committed_at": "2026-11-11T13:27:08Z"
    }
  ]
}
```

撤刊条目形如 `{"seq": 4, "stat_month": "2026-10", "change_type":
"retraction", "table": null, ...}`，下游据此删除本地副本。令牌与游标
不匹配或令牌损坏返回 HTTP 400；参数越界（如 `page_size=0`）返回 422。

### Swagger 演练（docker compose 启动后）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs
```

1. 在 `POST /api/v1/monthly-reports` 选内置样例 **“发布/更正：提交整表”**
   执行，记下返回的 `seq`；可再提交一个其它统计月。
2. 在 `GET /api/v1/monthly-reports/sync` 填 `after_seq=0`、`page_size=1`
   取首页，复制 `next_page_token`。
3. 回到 POST 用同 `stat_month` 再发一笔整表（补发更正）。
4. 用原参数重试首页：结果不变（轮次已冻结）；带 `page_token` 续页拿到
   末页，记录 `cutoff_seq`。
5. 用 `after_seq=<上一步 cutoff_seq>` 开新一轮，即可看到补发的更正。
6. 提交撤刊样例后再开一轮，返回 `change_type=retraction`、`table=null`。

## 算法说明

1. **建模**：每个遮盖格对应一个非负整数变量，行/列合计构成等式约束，
   即整数运输问题。
2. **可行性**：运输约束矩阵是全幺模矩阵，右侧为整数，线性规划松弛的
   顶点天然为整数解。服务先用自实现的 Dinic 最大流判定可行性：
   source→行（剩余行需求）、隐藏格所在行列之间建容量无限边、列→sink
   （剩余列供给）。最大流达不到隐藏格总流量即无解，并由最小割给出
   Hall 型冲突见证（哪些行的需求超出其邻接列供给）。
3. **求界**：可行时对每个隐藏格分别以该变量系数 +1 / −1 为目标做
   `scipy.optimize.linprog(method="highs")`，得到 `min` / `max`，
   并输出取扫描序第一个隐藏格的两张极值完整矩阵。
4. **复核**：输出矩阵在返回前重新逐行逐列核对公开合计、非负性与整数性。
5. **可还原判定**：`min == max` 即该格取值唯一，可被公开合计反推。

复杂度：设隐藏格数为 h（h ≤ 144），共 2h+1 次小规模 LP 与一次
O(V²E) 最大流；12×12 实测在百毫秒内完成。

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py            # 若 ensurepip 不可用
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```

测试集包含 70 组随机小规模问题的**暴力枚举交叉验证**：LP 求出的每个
区间与枚举全部可行赋值的 min/max 完全一致，并覆盖无解、可还原、
零矩阵、1×1、12×12 与各类 422/409 场景。
