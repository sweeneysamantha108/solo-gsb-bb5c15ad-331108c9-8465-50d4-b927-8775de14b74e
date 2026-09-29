# Rule decision service

一个基于 **FastAPI + SQLite + Docker** 的规则决策服务：

- 用户创建**规则集**，规则可通过表达式引用其他规则的命中结果，形成**条件依赖图**；
- 提交文档（任意 JSON）进行**决策计算**，按依赖图迭代到**稳定（定点）结果**；
- 返回命中的规则顺序、决策值以及**冲突解释**（为什么某条命中规则被压制）；
- 保存时**检测循环依赖**并拒绝保存（HTTP 400，错误码 `CYCLIC_DEPENDENCY`）；
- 规则集**版本化**：每次修改生成不可变新版本并自动重算最新版，旧版本与旧结果永久保留可查。

## 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 应用入口 / HTTP API
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── database.py        # SQLite 连接、建表、初始化
│   ├── crud.py            # 持久化层（规则集 / 版本 / 决策结果）
│   ├── engine/
│   │   ├── ast_eval.py    # 基于 AST 的安全表达式求值（沙箱）
│   │   ├── compiler.py    # 规则编译：引用、依赖、循环、冲突策略校验
│   │   ├── evaluator.py   # 拓扑排序 + 定点迭代 + 冲突处理
│   │   ├── coverage.py    # 多文档覆盖分析：覆盖单元/冲突场景 + 贪心代表输入
│   │   ├── impact.py      # 静态影响分析：反向依赖闭包 + 冲突组联动
│   │   ├── footprint.py   # 静态输入足迹：doc 访问链 → RFC 6901 JSON Pointer
│   │   └── diff.py        # 跨版本预演：决策 JSON 路径差异 + 命中规则/冲突摘要
│   └── seed.py            # 演示规则集初始化
├── tests/                 # pytest 单元/接口测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8000，自动建库并写入演示规则集 demo-promotions
```

### Docker 直接运行（不使用 Compose）

```bash
# 构建镜像
docker build -t rule-decision-service:latest .

# 启动容器；用命名卷持久化 SQLite 数据
docker run -d \
  --name rule-decision-service \
  -p 8000:8000 \
  -e SEED_DEMO=1 \
  -e DATABASE_URL='sqlite:////data/rules.db' \
  -v rule-data:/data \
  rule-decision-service:latest

# 查看日志 / 停止 / 重新启动
docker logs -f rule-decision-service
docker stop rule-decision-service
docker start rule-decision-service
```

镜像说明：

- 容器内监听 `8000`，用 `-p 8000:8000` 映射到宿主机；
- 数据库默认写容器内 `/data/rules.db`（声明为 VOLUME），生产环境务必挂载
  命名卷或宿主机目录，否则容器删除后数据丢失；
- 环境变量：
  - `SEED_DEMO=1`：首次启动写入演示规则集 `demo-promotions`（默认已开启）；
  - `DATABASE_URL`：SQLite 路径，形如 `sqlite:////data/rules.db`；
- 镜像内置 `/health` 健康检查（`HEALTHCHECK`），可用
  `docker ps` 或 `docker inspect` 观察健康状态。


健康检查与交互文档：

- 健康检查：<http://localhost:8000/health>
- Swagger UI：<http://localhost:8000/docs>
- OpenAPI：<http://localhost:8000/openapi.json>

### 本地运行

```bash
pip install -r requirements.txt
SEED_DEMO=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件默认 `./data/rules.db`，可用环境变量 `DATABASE_URL` 修改。

## 规则模型

规则集（ruleset）：

| 字段 | 说明 |
| --- | --- |
| `name` / `description` | 名称与描述 |
| `default_conflict_policy` | 默认冲突策略：`collect_all` / `priority` / `first_hit` |
| `rules` | 规则列表（见下） |

规则（rule）：

| 字段 | 说明 |
| --- | --- |
| `code` | 规则编码，`[a-zA-Z_][a-zA-Z0-9_]*`，规则集内唯一 |
| `name` / `description` | 名称与描述 |
| `priority` | 优先级，数值越大越优先（默认 0） |
| `conflict_group` | 冲突组标识；同一组内命中的规则互相冲突 |
| `conflict_policy` | 本组冲突策略，缺省继承规则集默认值 |
| `depends_on` | 依赖的其他规则编码（计算顺序保证 + 循环检测依据） |
| `condition` | 条件表达式，求值为真则命中。空表达式视为恒真 |
| `action` | 动作表达式，命中时计算，作为该规则的决策值 |

### 表达式语言

条件/动作是受限的 Python 表达式，由 AST 白名单沙箱执行：

- 文档通过变量 `doc` 访问，例如 `doc["amount"] >= 100`；
- 其他规则的命中结果可通过 `规则编码.命中` 访问，例如：
  - `is_vip.hit` —— 规则 `is_vip` 是否命中（布尔）；
  - `is_vip.value` —— 规则 `is_vip` 动作的返回值；
- 支持 `and / or / not`、比较运算、算术、三元表达式、列表/字典字面量、
  `in / not in`；
- 允许的函数：`len / str / int / float / bool / round / min / max / sum / abs / lower / upper / contains`；
  允许的字符串方法：`lower / upper / startswith / endswith / contains`。
- **禁止**属性穿透、导入、调用任意函数、下标越界（缺省键一律得到 `None`）。

> 条件中引用其他规则时，被引用的规则编码必须出现在 `depends_on` 中，
> 否则保存时报 `UNDECLARED_REFERENCE`。

### 冲突策略

同一 `conflict_group` 中多条规则同时命中时：

- `collect_all`（默认）：全部保留，决策值按字段合并（字段互相覆盖时给出解释）；
- `priority`：只保留 `priority` 最高者，其余标记为 `suppressed`（附原因）；
- `first_hit`：只保留依赖拓扑序中的第一条，其余标记为 `suppressed`。

## 决策语义：依赖图与定点

1. 按 `depends_on` 做拓扑排序得到确定的评估顺序（无法排序即存在环，拒绝保存）；
2. 每一轮按拓扑序评估全部规则，上一轮结果作为 `rule.hit / rule.value` 输入；
3. 重复评估直到所有规则的命中状态与动作值不再变化（定点）；
4. 在命中集合上执行冲突处理，输出最终决策、命中顺序与冲突解释。

响应示例：

```json
{
  "id": "9f1c...",
  "ruleset_id": "demo-promotions",
  "version": 2,
  "document": {"amount": 1500, "customer": {"vip": true}},
  "fired_rule_codes": ["is_vip", "large_order", "vip_discount", "free_shipping"],
  "hit_rule_codes": ["vip_discount", "free_shipping"],
  "evaluation_order": ["is_vip", "large_order", "vip_discount", "free_shipping"],
  "iterations": 2,
  "converged": true,
  "decision": {"discount": 0.15, "free_shipping": true},
  "conflicts": [
    {
      "group": "discount",
      "policy": "priority",
      "winner": "vip_discount",
      "suppressed": [
        {"rule_code": "large_order", "reason": "lower_priority", "priority": 10}
      ]
    }
  ],
  "traces": [ {"rule_code": "vip_discount", "hit": true, "value": {"discount": 0.15}} ],
  "created_at": "2026-09-21T00:00:00"
}
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 健康检查 |
| `POST` | `/api/rulesets` | 创建规则集（编译校验，含循环依赖检测） |
| `GET`  | `/api/rulesets` | 列出全部规则集 |
| `GET`  | `/api/rulesets/{id}` | 获取规则集元数据 |
| `GET`  | `/api/rulesets/{id}/versions` | 列出全部版本 |
| `GET`  | `/api/rulesets/{id}/versions/{version}` | 获取指定版本快照 |
| `GET`  | `/api/rulesets/{id}/versions/{version}/snapshot` | 导出该版本为快照迁移 JSON 包（源标识 + 完整定义 + 内容摘要） |
| `POST` | `/api/rulesets/{id}/snapshot-import` | 导入快照迁移包：核对摘要并重编译，幂等创建/匹配/追加版本 |
| `POST` | `/api/rulesets/{id}/impact` | 静态依赖影响分析（不求值、不落库） |
| `POST` | `/api/rulesets/{id}/input-footprint` | 静态输入足迹：汇总规则及上游读取的文档路径（不求值、不落库） |
| `PUT`  | `/api/rulesets/{id}` | 修改规则集 → 生成新版本并自动重算 |
| `POST` | `/api/rulesets/{id}/evaluate` | 用最新版（或 `?version=n` 指定旧版）决策，默认持久化结果；可携带 `Idempotency-Key` 请求头做客户端幂等（见下） |
| `POST` | `/api/rulesets/{id}/evaluate-batch` | 批量决策（≤100 项），整批固定同一版本，逐项隔离错误 |
| `POST` | `/api/rulesets/{id}/coverage` | 多文档覆盖分析（1～100 份，不落库）：命中/未命中规则、已观察冲突场景与贪心代表输入 |
| `POST` | `/api/rulesets/{id}/compare` | 发布前预演：同一文档按两个版本求值（不落库），返回两版决策与差异摘要 |
| `GET`  | `/api/rulesets/{id}/evaluations` | 列出该规则集的历史决策结果 |
| `GET`  | `/api/rulesets/{id}/evaluation-stats` | 聚合已持久化决策：规则命中率与决策字段统计（支持 `version` / `since` / `until` 筛选） |
| `GET`  | `/api/evaluations/{eval_id}` | 按 ID 查询单次决策结果 |
| `POST` | `/api/rulesets/{id}/recompute` | 用旧文档对最新版重算（保留旧结果） |

### 三分钟试用

```bash
# 1) 对演示规则集提交文档（大额 + VIP）
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"document": {"amount": 1500, "customer": {"vip": true}, "region": "north"}}' | python3 -m json.tool

# 2) 普通小单
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"document": {"amount": 80, "region": "north"}}' | python3 -m json.tool

# 3) 查询历史结果
curl -s localhost:8000/api/rulesets/demo-promotions/evaluations | python3 -m json.tool
```

### 单文档决策的客户端幂等键

对**单文档持久化决策** `POST /api/rulesets/{id}/evaluate`（`persist` 默认为
true），可通过 HTTP 请求头 **`Idempotency-Key`** 传入客户端幂等键，保证
“同一笔决策”因网络重试、客户端重发或并发提交时**只求值、只落库一次**。

行为语义：

- **首次受理**：固定本次实际使用的规则版本，将规则集、幂等键、请求的**版本
  选择方式**、**按对象键排序规范化的文档**以及完整决策结果关联保存（键按
  规则集隔离，不同规则集可复用同名键）；
- **同键重试 / 并发重试**：同一规则集、同一键、相同请求（文档与版本选择方式
  都一致）即使并发到达或期间规则集发布了新版本，也直接返回**首次结果与原记录
  ID**，不重复求值、不重复落库；
- **同键冲突**：键相同但**文档不同**，或**版本选择方式不同**（`?version=n`
  指定版本 vs 缺省最新版；以及指定了不同的 `n`），返回 **HTTP 409**
  `IDEMPOTENCY_KEY_CONFLICT`，`details.field` 区分 `document` /
  `version_selection`，并在后者中回显首次与本次的选择方式；
- **失败不占键**：求值失败（如 `NOT_CONVERGED`）返回 422 且**不写入幂等记录**，
  同一键可被后续成功请求使用；
- **未携带键**：保持原有行为——每次调用都重新求值并产生新记录；
- **非持久化调用携带键**：`persist=false` 时携带 `Idempotency-Key` 被**明确
  拒绝**（HTTP 400 `IDEMPOTENCY_KEY_REQUIRES_PERSIST`）；
- 幂等键为最多 255 字符的非空字符串；纯空白头按未携带处理；
- 成功响应结构与普通 `evaluate` **完全一致**（`EvaluationOut`），规则集或
  指定版本不存在仍返回原有的 404 `NOT_FOUND` / `VERSION_NOT_FOUND`。

> 文档规范化：对象按键递归排序（`{"b":2,"a":1}` 与 `{"a":1,"b":2}` 视为
> 同一请求），数组按下标保序（顺序属于语义）。

请求示例（携带幂等键）：

```bash
# 首次受理：固定版本（假设当前为 v1）并落库，返回记录 ID
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260929-0001' \
  -d '{"document": {"amount": 1500, "customer": {"vip": true}}}'
# -> {"id": "c699...", "version": 1, "decision": {...}, ...}

# 重试 / 重发（即使键序写法不同，规范化后等价）
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260929-0001' \
  -d '{"document": {"customer": {"vip": true}, "amount": 1500}}'
# -> 200，返回同一 id、同一 version=1 与首次完整结果；不会产生第二条记录

# 期间规则集发布了 v2：缺省“最新版”的重试仍返回首次固定的 v1 结果
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260929-0001' \
  -d '{"document": {"amount": 1500, "customer": {"vip": true}}}'
# -> 200，version 仍为首次的 1，id 不变
```

同键不同文档 → 409：

```bash
curl -i -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260929-0001' \
  -d '{"document": {"amount": 80}}'
# HTTP/1.1 409 Conflict
# {"error": {"code": "IDEMPOTENCY_KEY_CONFLICT",
#   "message": "幂等键已用于不同的请求文档",
#   "details": {"idempotency_key": "order-20260929-0001", "field": "document"}}}
```

同键不同版本选择方式 → 409（首次缺省最新版，本次指定 `?version=2`）：

```bash
curl -i -X POST 'localhost:8000/api/rulesets/demo-promotions/evaluate?version=2' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260929-0001' \
  -d '{"document": {"amount": 1500, "customer": {"vip": true}}}'
# HTTP/1.1 409 Conflict
# {"error": {"code": "IDEMPOTENCY_KEY_CONFLICT",
#   "message": "幂等键已用于不同的版本选择方式",
#   "details": {"idempotency_key": "order-20260929-0001",
#     "field": "version_selection",
#     "original": {"mode": "latest", "version": null},
#     "conflicting": {"mode": "pinned", "version": 2}}}}
```

非持久化调用携带键 → 400：

```bash
curl -i -X POST 'localhost:8000/api/rulesets/demo-promotions/evaluate?persist=false' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260929-0001' \
  -d '{"document": {"amount": 1500}}'
# HTTP/1.1 400 Bad Request
# {"error": {"code": "IDEMPOTENCY_KEY_REQUIRES_PERSIST",
#   "message": "非持久化调用（persist=false）不支持幂等键", "details": {}}}
```

> 幂等能力仅用于**单文档** `evaluate`；批量（evaluate-batch）、覆盖
> （coverage）、预演（compare）、重算（recompute）不接受幂等键。

### 批量评估

`POST /api/rulesets/{id}/evaluate-batch` 一次提交多份文档：

- 请求体 `documents` 为 JSON 数组，**单次最多 100 项**（超出返回 HTTP 422）；
- 可选 `version`（请求体内）指定规则版本，缺省使用最新版；**整批固定使用
  同一规则版本**，只解析编译一次；
- 每项**独立求值**：某项求值失败只记录该项错误，**不中止整批**；
- 响应按输入 `index` 逐项返回，并汇总 `success_count` / `error_count`；
- 与单次评估一致，默认持久化各项成功结果；加 `?persist=false` 只算不存
  （失败项不会落库）；
- 规则集或指定版本不存在属于整批级错误，直接返回 HTTP 404。

请求示例：

```bash
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate-batch \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "documents": [
      {"amount": 1500, "customer": {"vip": true}, "region": "north"},
      {"amount": 80, "region": "north"},
      {"amount": 10, "coupons": ["SAVE10", "SAVE20"]}
    ]
  }' | python3 -m json.tool
```

响应示例（每项结构与单次 `evaluate` 一致；此处 `result` 有省略）：

```json
{
  "ruleset_id": "demo-promotions",
  "version": 1,
  "total": 3,
  "success_count": 3,
  "error_count": 0,
  "results": [
    {
      "index": 0,
      "status": "success",
      "result": {
        "id": "9f1c...",
        "ruleset_id": "demo-promotions",
        "version": 1,
        "decision": {"discount": 0.15, "free_shipping": true},
        "fired_rule_codes": ["is_vip", "large_order", "vip_discount", "free_shipping"],
        "hit_rule_codes": ["vip_discount", "free_shipping"],
        "converged": true,
        "iterations": 2
      },
      "error": null
    },
    {
      "index": 1,
      "status": "success",
      "result": {"decision": {}, "fired_rule_codes": [], "converged": true},
      "error": null
    },
    {
      "index": 2,
      "status": "success",
      "result": {"decision": {"coupon": "SAVE10"}, "fired_rule_codes": ["coupon_save10", "coupon_save20"]},
      "error": null
    }
  ]
}
```

某项求值失败时，该项 `status` 为 `error`、`result` 为 `null`，错误信息在
`error` 中，其他项照常求值：

```json
{
  "index": 2,
  "status": "error",
  "result": null,
  "error": {
    "code": "NOT_CONVERGED",
    "message": "规则评估在 100 轮内未达到稳定状态",
    "details": {"max_iterations": 100}
  }
}
```

> 注意：批量接口在存在失败项时仍返回 **HTTP 200**，通过每项的
> `status` / `error` 与顶层计数区分成败；仅当规则集或版本不存在等
> 整批级问题发生时才返回 4xx。

### 多文档覆盖分析

`POST /api/rulesets/{id}/coverage` 用于在**一组样本文档上度量规则覆盖**：
提交 1～100 份文档与一个**必填、不可变的 `version`**，服务只解析编译一次，
对每份文档**逐项独立求值、全程不写入 evaluations**；单项求值失败只记录在
该项 `error` 中（按原下标返回），不中止其余项，也不产生覆盖证据。

覆盖单元（coverage unit）是一次成功求值中可观察到的最小证据，分三类：

| `kind` | 身份构成 | 来源 |
| --- | --- | --- |
| `rule` | 规则编码 | `fired_rule_codes` 中的每条命中规则（含被压制者） |
| `group` | 冲突组 + 胜出方 + 被压制规则编码集合（按编码排序） | `conflicts` 中 priority / first_hit 的整组裁决 |
| `field` | 字段名 + 前写入方 + 最终写入方 | `conflicts` 中 collect_all 的同名字段覆盖 |

冲突解释涉及的规则（胜出方、被压制方、字段覆盖前后写入方）同时以 `rule`
单元计入；组内只有一条规则命中时同样产生一条“无被压制者”的 `group` 单元。

响应字段：

- `items`：逐项结果，按**原输入下标**返回。成功项回显 `fired_rule_codes`、
  原始 `conflicts` 与该文档的全部 `coverage_units`；失败项这三者为空，
  错误结构与批量接口一致（`NOT_CONVERGED` 等）；
- `rule_coverage`：该版本**全部规则**（按 `evaluation_order` 排列）在成功
  文档中的命中次数 `hit_count` 与命中文档下标 `covered_by_indexes`；
- `uncovered_rule_codes`：所有成功文档均未命中的规则编码（按
  `evaluation_order` 排列；失败文档不计入）；
- `conflict_scenarios`：按覆盖单元身份**去重后的已观察冲突场景**，按首次
  观察顺序排列；每项含组/字段、策略、胜出方、被压制规则、出现次数
  `observed_count`、出现下标 `observed_at_indexes`（升序）以及该场景贡献的
  `coverage_units`；
- `representative_indexes`：**确定性贪心**选出的代表输入下标——每轮选择
  “相对尚未覆盖集合新增覆盖单元最多”的成功项，并列时取**最小输入下标**，
  直到没有任何成功项能带来新增覆盖为止；**失败项不参与选择**，因此全部
  失败或全部零覆盖时为空列表。

错误情况：规则集不存在 → **404** `NOT_FOUND`；版本不存在 → **404**
`VERSION_NOT_FOUND`；缺少 `version`、`version<1`、`documents` 为空或超过
100 项 → **422**。单项求值失败不改变 HTTP 状态码（仍为 200）。

请求示例（`g` 组使用 priority：r_b 优先于 r_a；另有独立规则 r_c）：

```bash
curl -s -X POST localhost:8000/api/rulesets/cov-groups/coverage \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "documents": [
      {"x": 1},
      {"x": 2},
      {"y": true},
      {"x": 2, "y": true}
    ]
  }' | python3 -m json.tool
```

响应示例（有省略）：

```json
{
  "ruleset_id": "cov-groups",
  "version": 1,
  "total": 4,
  "success_count": 4,
  "error_count": 0,
  "rule_coverage": [
    {"code": "r_a", "hit_count": 3, "covered_by_indexes": [0, 1, 3]},
    {"code": "r_b", "hit_count": 2, "covered_by_indexes": [1, 3]},
    {"code": "r_c", "hit_count": 2, "covered_by_indexes": [2, 3]}
  ],
  "uncovered_rule_codes": [],
  "conflict_scenarios": [
    {
      "kind": "group",
      "group": "g",
      "field": null,
      "policy": "priority",
      "winner": "r_a",
      "suppressed_rule_codes": [],
      "previous_winner": null,
      "reason": null,
      "observed_count": 1,
      "observed_at_indexes": [0],
      "coverage_units": [
        {"kind": "rule", "rule_code": "r_a", "group": null, "field": null,
         "winner": null, "previous_winner": null, "suppressed_rule_codes": []},
        {"kind": "group", "rule_code": null, "group": "g", "field": null,
         "winner": "r_a", "previous_winner": null, "suppressed_rule_codes": []}
      ]
    },
    {
      "kind": "group",
      "group": "g",
      "field": null,
      "policy": "priority",
      "winner": "r_b",
      "suppressed_rule_codes": ["r_a"],
      "observed_count": 2,
      "observed_at_indexes": [1, 3],
      "coverage_units": [
        {"kind": "rule", "rule_code": "r_a"},
        {"kind": "rule", "rule_code": "r_b"},
        {"kind": "group", "group": "g", "winner": "r_b",
         "suppressed_rule_codes": ["r_a"]}
      ]
    }
  ],
  "representative_indexes": [3, 0],
  "items": [
    {"index": 0, "status": "success",
     "fired_rule_codes": ["r_a"],
     "conflicts": [{"group": "g", "policy": "priority", "winner": "r_a",
                    "suppressed": []}],
     "coverage_units": [
       {"kind": "rule", "rule_code": "r_a"},
       {"kind": "group", "group": "g", "winner": "r_a",
        "suppressed_rule_codes": []}
     ],
     "error": null}
  ]
}
```

上例的贪心过程：下标 3 一份文档同时覆盖 r_a/r_b/r_c 三条 rule 单元与
`g→r_b 压制 r_a` 场景，首轮入选；剩余的 `g→r_a（无被压制）` 场景只在
下标 0 出现，次轮入选；下标 1、2 不再带来新增覆盖。

某项求值失败时（仍返回 HTTP 200）：

```json
{
  "index": 1,
  "status": "error",
  "fired_rule_codes": [],
  "conflicts": [],
  "coverage_units": [],
  "error": {
    "code": "NOT_CONVERGED",
    "message": "规则评估在 100 轮内未达到稳定状态",
    "details": {"max_iterations": 100}
  }
}
```

### 发布前跨版本预演

`POST /api/rulesets/{id}/compare` 在**发布新版本前**用同一份文档分别按
两个不可变版本快照求值，对比决策差异：

- 请求体指定 `base_version`（基准版）、`target_version`（待发布版）与
  `document`（缺省为 `{}`）；`base_version` 与 `target_version` 均必填且 ≥ 1；
- 两版**各自独立求值，不写入 evaluations**（两侧记录的 `id` 均为空，
  与 `evaluate?persist=false` 同构）；
- 响应的 `base` / `target` 各自带 `version`、`status`、完整 `result` 与
  `error`；**任一侧求值失败只体现在该侧 `error`（结构与批量接口的单项
  错误一致），另一侧照常求值**，HTTP 状态码仍为 200，此时 `summary` 与
  `decision_diff` 均为 `null`；
- 两侧均成功时，`summary` 给出命中规则与冲突的跨版本摘要，
  `decision_diff` 给出按 **JSON Pointer 路径**归并的决策差异。

`summary` 字段：

- `fired_rules` / `hit_rules`：分别比较“求值为真的规则（含被冲突策略
  压制者）”与“冲突处理后最终生效的规则”。每项含 `base`、`target`
  （保序回显）以及相对基准版的 `added` / `removed`；
- `conflicts`：`base` / `target` 回显两侧全部冲突解释；整组冲突按
  `group`、`collect_all` 的字段覆盖（`group` 为 null）按 `field` 归并，
  分别给出 `added` / `removed` / `changed`（同组但 `policy`、胜出方或被
  压制规则集合变化记为 `changed`，条目形如 `{group, field, old, new}`）。

`decision_diff` 的路径归并规则：

- 双方同路径且同为对象或同为数组时递归比较：对象按键、数组按下标对齐；
- 只在一侧出现的子树**逐叶展开**（新增子树挂在 `added`、消失子树挂在
  `removed`，每条带 `old`/`new`，缺席一侧为 `null`）；空对象/空数组占
  自身路径；
- 同路径类型不同（对象与数组、布尔与数值等）或标量值不同记为 `changed`
  （注意 JSON 语义下 `true` 与 `1` 视为不同值）；
- 路径按 **RFC 6901** 转义（`~` → `~0`、`/` → `~1`），三类列表均按路径
  字典序排列；两版决策完全相同时三个列表均为空。

错误情况：规则集不存在 → **404** `NOT_FOUND`；`base_version` 或
`target_version` 不存在 → **404** `VERSION_NOT_FOUND`；请求体缺字段、
版本号 < 1 → **422**。两侧文档求值失败属于**单侧错误**，返回 200 并在
对应侧的 `error` 中给出（`NOT_CONVERGED` 等现有错误码）。

请求示例（v1 阈值 10、含 r2；v2 阈值收紧到 1000、新增 r3）：

```bash
curl -s -X POST localhost:8000/api/rulesets/my-set/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "base_version": 1,
    "target_version": 2,
    "document": {"x": 42}
  }' | python3 -m json.tool
```

响应示例（有省略）：

```json
{
  "ruleset_id": "my-set",
  "base_version": 1,
  "target_version": 2,
  "base": {
    "version": 1,
    "status": "success",
    "result": {"id": "", "version": 1, "decision": {"level": "high", "bonus": 1},
               "fired_rule_codes": ["r1", "r2"], "conflicts": []},
    "error": null
  },
  "target": {
    "version": 2,
    "status": "success",
    "result": {"id": "", "version": 2, "decision": {"extra": {"n": 7}},
               "fired_rule_codes": ["r3"], "conflicts": []},
    "error": null
  },
  "summary": {
    "fired_rules": {"base": ["r1", "r2"], "target": ["r3"],
                    "added": ["r3"], "removed": ["r1", "r2"]},
    "hit_rules": {"base": ["r1", "r2"], "target": ["r3"],
                  "added": ["r3"], "removed": ["r1", "r2"]},
    "conflicts": {"base": [], "target": [],
                  "added": [], "removed": [], "changed": []}
  },
  "decision_diff": {
    "added": [{"path": "/extra/n", "old": null, "new": 7}],
    "removed": [
      {"path": "/bonus", "old": 1, "new": null},
      {"path": "/level", "old": "high", "new": null}
    ],
    "changed": []
  }
}
```

一侧求值失败时的响应示例：

```json
{
  "base_version": 1,
  "target_version": 2,
  "base": {"version": 1, "status": "success",
           "result": {"decision": {"ok": 1}}, "error": null},
  "target": {"version": 2, "status": "error", "result": null,
             "error": {"code": "NOT_CONVERGED",
                       "message": "规则评估在 100 轮内未达到稳定状态",
                       "details": {"max_iterations": 100}}},
  "summary": null,
  "decision_diff": null
}
```

### 评估统计

`GET /api/rulesets/{id}/evaluation-stats` 聚合该规则集**已持久化**的决策结果
（`persist=false` 的试算与求值失败项均不计入）：

- 可选筛选参数：
  - `version`：只统计指定规则版本的决策；
  - `since` / `until`：ISO-8601 时间，按决策创建时间过滤，**含边界**；
    无时区的输入按 UTC 处理，带偏移的输入会归一到 UTC；
- `total_evaluations`：筛选后命中的决策条数；
- `rule_stats`：按规则 `code` 排序。`hit_count` 为 traces 中 `hit=true` 的
  决策条数（**被冲突策略压制的命中也计入**）；`hit_rate =
  hit_count / total_evaluations`，保留 4 位小数；
- `decision_field_counts`：按字段名排序，统计最终 `decision` 的顶层字段
  在多少条决策中出现；
- 规则集不存在返回 404（`NOT_FOUND`）；无匹配记录返回零值结构
  （`total_evaluations` 为 0、两个列表为空）；非法时间参数返回 422。

```bash
curl -s 'localhost:8000/api/rulesets/demo-promotions/evaluation-stats?since=2026-01-01T00:00:00Z' \
  | python3 -m json.tool
```

响应示例：

```json
{
  "ruleset_id": "demo-promotions",
  "version": null,
  "since": "2026-01-01T00:00:00+00:00",
  "until": null,
  "total_evaluations": 2,
  "rule_stats": [
    {"code": "free_shipping", "hit_count": 1, "hit_rate": 0.5},
    {"code": "is_vip", "hit_count": 1, "hit_rate": 0.5},
    {"code": "large_order", "hit_count": 1, "hit_rate": 0.5},
    {"code": "vip_discount", "hit_count": 1, "hit_rate": 0.5}
  ],
  "decision_field_counts": [
    {"field": "discount", "count": 1},
    {"field": "free_shipping", "count": 1}
  ]
}
```

### 循环依赖被拒绝

```bash
curl -i -X POST localhost:8000/api/rulesets -H 'Content-Type: application/json' -d '{
  "name": "cyclic",
  "rules": [
    {"code": "a", "condition": "b.hit",        "depends_on": ["b"]},
    {"code": "b", "condition": "a.hit == true","depends_on": ["a"]}
  ]
}'
# HTTP/1.1 400 Bad Request
# {"error": {"code": "CYCLIC_DEPENDENCY", "message": "规则依赖存在循环：a -> b -> a", "cycle": ["a","b","a"]}}
```

### 静态依赖影响分析

`POST /api/rulesets/{id}/impact` 用于在**改动规则前评估影响面**：提交
`version` 与一个或多个 `rule_codes`，服务基于该版本快照中**已编译**的
`depends_on` 与条件表达式引用，静态计算反向传递依赖闭包——**不执行任何
文档求值，也不会写入 evaluations**。

请求体：

| 字段 | 说明 |
| --- | --- |
| `version` | **必填**，分析所基于的规则版本（不可变快照） |
| `rule_codes` | 一个或多个待分析的规则编码（至少 1 个；重复编码去重保序） |

响应字段：

- `rule_codes`：去重保序后的输入回显；
- `direct_dependents`：**直接依赖**任一输入规则的规则（反向边 1 跳）；
- `transitive_dependents`：**传递依赖**（反向边 ≥2 跳）的规则；多路径可达时
  按最短路径计层级，因此已计入 direct 的规则不会再出现在这里；
- `affected`：输入规则（`level=0`）与上述两类受影响规则的并集，按
  `evaluation_order` 排列，每项含 `level`（0/1/≥2）与 `relation`
  （`input` / `direct_dependent` / `transitive_dependent`）；
- `coupled_rules`：与任一受影响规则（含输入）处于**同一 `conflict_group`**
  的其余规则。它们不经依赖边被波及（`level` 恒为 `null`），但运行时可能因
  冲突策略联动（被压制或反向胜出）；`coupled_with` 指出 evaluation_order 上
  第一个触发联动的受影响规则。若某条联动规则本身已在依赖闭包中，则依赖分类
  优先，不在此重复出现；联动关系也不会继续向外传播。

所有规则列表均按该版本的 `evaluation_order` 排列，保证输出确定。

错误情况：

- 规则集不存在 → **404** `NOT_FOUND`；
- 版本不存在 → **404** `VERSION_NOT_FOUND`；
- 规则编码在该版本中不存在 → **422** `RULE_NOT_FOUND`
  （`details.rule_codes` 列出全部缺失编码）；
- 请求体缺字段、`version<1`、`rule_codes` 为空 → **422**。

请求示例（演示规则集 v1 中，`is_vip` 被 `vip_discount` 引用，后者又被
`manual_review` 引用；`vip_discount` 与 `large_order` 同属 discount 组）：

```bash
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/impact \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "rule_codes": ["is_vip"]}' | python3 -m json.tool
```

响应示例：

```json
{
  "ruleset_id": "demo-promotions",
  "version": 1,
  "rule_codes": ["is_vip"],
  "affected": [
    {"rule_code": "is_vip", "level": 0, "relation": "input"},
    {"rule_code": "vip_discount", "level": 1, "relation": "direct_dependent"},
    {"rule_code": "manual_review", "level": 2, "relation": "transitive_dependent"}
  ],
  "direct_dependents": ["vip_discount"],
  "transitive_dependents": ["manual_review"],
  "coupled_rules": [
    {
      "rule_code": "large_order",
      "conflict_group": "discount",
      "coupled_with": "vip_discount",
      "level": null
    }
  ]
}
```

一次提交多个规则时，各规则的闭包取并集；例如同时分析
`["large_order", "is_vip"]` 会得到直接依赖方
`["vip_discount", "free_shipping"]` 与传递依赖方 `["manual_review"]`，
而 discount 组双方都已在闭包内，不再产生联动项。

### 静态输入足迹分析

`POST /api/rulesets/{id}/input-footprint` 用于在**提交文档前**静态回答
“这些规则运行时会读取文档的哪些字段”：对每个目标规则，汇总**规则自身
及其全部上游依赖规则**（沿 `depends_on` 与表达式规则引用展开）在
`condition` / `action` 中读取的文档路径。分析过程**不执行任何文档求值
（不调用表达式求值器、不发生短路裁剪），也不写入 evaluations**——三元
表达式的两个分支、`and/or` 两侧等实际未必执行的读取都会保守计入。

请求体与 `/impact` 相同：

| 字段 | 说明 |
| --- | --- |
| `version` | **必填**，分析所基于的规则版本（不可变快照） |
| `rule_codes` | 一个或多个目标规则编码（至少 1 个；重复编码去重保序） |

路径识别规则：

- 只接受**以 `doc` 为根的访问链**，每一级下标必须是**字符串或非负整数
  常量**（`doc["amount"]`、`doc["items"][0]`、`doc["a~b/c"]`）；字典点
  访问 `doc.amount` 在求值期等价于 `doc["amount"]`，同样接受；
- 收集**完整访问链**（叶子字段）：`doc["customer"]["vip"]` 的路径是
  `/customer/vip`，中间节点 `doc["customer"]` 不单独列出；方法调用在方法
  名之前终止（`doc["name"].upper()` 的路径是 `/name`）；单独出现的裸
  `doc`（如 `len(doc)`）记为 `""`，即整个文档；
- 每个常量下标按 **RFC 6901** 转义为 JSON Pointer：`~` → `~0`、
  `/` → `~1`；非负整数按十进制原样作为引用词（`doc[0]` → `/0`）；
- **任何一级下标是动态/不支持形式**——名字或表达式（`doc[k]`、
  `doc["k" + "x"]`）、负下标（`doc[-1]`）、切片（`doc[1:3]`）、
  布尔/浮点/`None`、元组下标等——无法静态确定读取位置，请求**整体返回
  422 `UNSUPPORTED_DOCUMENT_ACCESS`，不会给出部分结果**；
- 不以 `doc` 为根的下标（如规则结果 `r1.value[0]`）不属于文档读取，
  既不报错也不计入。

响应中每个目标返回：

- `paths`：目标及其上游读取路径去重后的列表，**按 JSON Pointer 字典序**；
- 每条路径的 `used_by`：闭包内实际读取该路径的规则编码，按该版本的
  **`evaluation_order`** 排列去重。

错误情况与 `/impact` 一致：规则集不存在 → 404 `NOT_FOUND`；版本不存在 →
404 `VERSION_NOT_FOUND`；规则编码在该版本中不存在 → 422 `RULE_NOT_FOUND`
（`details.rule_codes` 列出全部缺失编码）；请求体缺字段、`version<1`、
`rule_codes` 为空 → 422。

请求示例：

```bash
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/input-footprint \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "rule_codes": ["manual_review"]}' | python3 -m json.tool
```

响应示例（`manual_review` 依赖 `vip_discount`，后者又依赖 `is_vip`；
`/amount` 同时被 `vip_discount` 与 `manual_review` 读取）：

```json
{
  "ruleset_id": "demo-promotions",
  "version": 1,
  "rule_codes": ["manual_review"],
  "footprints": [
    {
      "rule_code": "manual_review",
      "paths": [
        {"path": "/amount", "used_by": ["vip_discount", "manual_review"]},
        {"path": "/customer/vip", "used_by": ["is_vip"]}
      ]
    }
  ]
}
```

动态下标导致整体失败的示例：

```bash
curl -i -X POST localhost:8000/api/rulesets/r/input-footprint \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "rule_codes": ["r1"]}'
# HTTP/1.1 422 Unprocessable Entity
# {"error": {"code": "UNSUPPORTED_DOCUMENT_ACCESS",
#   "message": "规则 r1 的 condition 表达式包含无法静态确定的文档下标：doc[...\"k\" + \"x\"...]",
#   "details": {"rule_code": "r1", "part": "condition",
#               "index": "\"k\" + \"x\"", "expression": "doc[\"k\" + \"x\"] == 1"}}}
```

### 规则集快照迁移（导出 / 导入）

快照迁移用于把某个版本搬到另一套环境（或另一个规则集 ID）：导出得到一个
**自描述 JSON 包**，导入时先核对内容摘要、再重新编译，全部通过后才落库。

`GET /api/rulesets/{id}/versions/{version}/snapshot` 导出指定版本，包结构：

| 字段 | 说明 |
| --- | --- |
| `format` | 固定为 `"ruleset-snapshot"`，标识包类型 |
| `format_version` | 包格式版本，当前为 `1` |
| `source` | 源规则集标识 `{ruleset_id, version}`，仅用于溯源 |
| `exported_at` | 导出时间（UTC ISO-8601），不参与摘要 |
| `digest` | 规范化内容摘要 `{algorithm: "sha256", value}` |
| `ruleset` | 完整规则集定义：`name` / `description` / `default_conflict_policy` / `rules` |

摘要对 `ruleset` 规范化后的内容计算 sha256：仅含参与规则语义的字段
（规则按提交顺序、`depends_on` 去重保序、缺省字段补齐），以
`sort_keys`、无多余空白、UTF-8、非 ASCII 不转义的 JSON 序列化。
同一内容无论经创建、更新、导入还是从已存储快照重算，摘要都一致。

导出示例：

```bash
curl -s localhost:8000/api/rulesets/demo-promotions/versions/1/snapshot \
  -o snapshot.json
python3 -m json.tool snapshot.json
```

响应示例（规则字段有省略）：

```json
{
  "format": "ruleset-snapshot",
  "format_version": 1,
  "source": {"ruleset_id": "demo-promotions", "version": 1},
  "exported_at": "2026-09-28T12:00:00+00:00",
  "digest": {
    "algorithm": "sha256",
    "value": "9f2c8e…（64 位十六进制）"
  },
  "ruleset": {
    "name": "促销规则演示",
    "description": "VIP、大额订单、优惠券、包邮、人工审核示例。……",
    "default_conflict_policy": "collect_all",
    "rules": [
      {
        "code": "is_vip",
        "name": "VIP 客户",
        "description": "",
        "priority": 0,
        "conflict_group": null,
        "conflict_policy": null,
        "depends_on": [],
        "condition": "doc[\"customer\"][\"vip\"] == true",
        "action": "{\"vip\": true}"
      }
    ]
  }
}
```

`POST /api/rulesets/{id}/snapshot-import` 把快照包导入到路径指定的
**目标规则集 ID**（允许与 `source.ruleset_id` 不同；迁移不依赖源 ID）。
处理流程：

1. 校验 `format` / `format_version` 与摘要算法（仅 sha256）；
2. 对包内 `ruleset` 重新规范化并计算 sha256，与 `digest.value` 不符
   （包被篡改或传输损坏）返回 **400** `DIGEST_MISMATCH`；
3. 按包内完整规则定义**重新编译**——重复编码、未知依赖、隐式引用、
   循环依赖、冲突策略冲突等静态校验全部重跑，任一失败返回 **400**
   对应编译错误码；
4. 落库规则（整个写入在单事务内原子完成）：
   - 目标 ID **不存在** → 创建 v1，`action="created"`（HTTP **201**）；
   - 目标已存在且内容与其**任一历史版本**摘要一致 → 幂等返回该版本，
     `action="matched"`（HTTP 200，**不产生新版本**）；
   - 否则 `action="appended"`（HTTP 200），版本号为**目标规则集**
     `current_version + 1`。

> 版本号始终按**目标规则集**递增，**不采用**包内 `source.version`；
> 因此把源端 v3 导入一个空目标只会得到 v1。摘要核对或编译失败时
> **不写库**，目标规则集保持原状。

导入示例：

```bash
curl -s -X POST localhost:8000/api/rulesets/promotions-copy/snapshot-import \
  -H 'Content-Type: application/json' \
  --data @snapshot.json | python3 -m json.tool
```

首次导入到不存在的目标，返回 **201**（再次导入同一包返回 200
`matched`）：

```json
{
  "action": "created",
  "ruleset_id": "promotions-copy",
  "version": 1,
  "source": {"ruleset_id": "demo-promotions", "version": 1},
  "digest": {"algorithm": "sha256", "value": "9f2c8e…"},
  "snapshot": {
    "id": "…",
    "ruleset_id": "promotions-copy",
    "version": 1,
    "name": "促销规则演示",
    "default_conflict_policy": "collect_all",
    "evaluation_order": ["is_vip", "large_order", "vip_discount",
                         "coupon_save10", "coupon_save20",
                         "free_shipping", "manual_review"],
    "rules": [ … ],
    "created_at": "2026-09-28T12:01:00+00:00"
  }
}
```

错误情况：

| 场景 | HTTP | 错误码 |
| --- | --- | --- |
| 导出时规则集 / 版本不存在 | 404 | `NOT_FOUND` / `VERSION_NOT_FOUND` |
| 目标 ID 含非法字符 | 400 | `INVALID_ID` |
| 摘要核对失败（内容被改动 / 损坏） | 400 | `DIGEST_MISMATCH` |
| 摘要算法不受支持（非 sha256） | 422 | Pydantic 校验错误 |
| 重新编译失败（循环依赖等） | 400 | 对应编译错误码，如 `CYCLIC_DEPENDENCY` |
| 包缺字段 / `format` 不符 / 规则为空 | 422 | Pydantic 校验错误 |

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

测试覆盖：AST 沙箱安全性、编译校验（重复编码/未知依赖/隐式引用/循环/冲突策略冲突）、
拓扑排序、定点迭代、三种冲突策略、版本化重算、评估统计聚合（筛选/排序/零值/非法参数）、
静态影响分析（反向闭包/层级/多输入去重/冲突组联动/404 与 422）、
静态输入足迹（doc 访问链提取/RFC 6901 转义/上游闭包 used_by/动态下标 422/404 与 422）、
跨版本预演（JSON 路径差异归并/空差异/冲突胜出方变化/求值失败单侧隔离/不落库/404 与 422）、
多文档覆盖分析（rule/group/field 覆盖单元/命中与未命中统计/冲突场景去重与观察下标/
确定性贪心代表选择/失败项隔离且不参与选择/不落库/404 与 422）、
规则集快照迁移（导出包结构/规范化摘要/导入 created·matched·appended 幂等分支/
版本号按目标递增不采用包内版本/摘要不符与重编译失败不写库/400 与 404）、
单文档决策客户端幂等键（首次落库并固定版本/同键重放返回首次记录 ID/
新版本发布仍固定首次版本/文档按对象键排序规范化/同键不同文档或版本选择方式 409/
求值失败不占键/非持久化携带键 400/无键行为不变/键按规则集隔离/并发只落一条），
以及完整 HTTP 接口。
