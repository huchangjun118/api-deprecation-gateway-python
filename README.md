# API Deprecation Gateway

为 FastAPI 服务提供一层**接口版本协商**与**弃用生命周期管理**能力。服务对外不再暴露单一版本行为：客户端可以通过多种来源表达期望版本，版本策略支持运行期热更新，弃用版本带标准提示头，已下线版本被明确拒绝并给出去向指引。

- 保持现有可用接口行为不变（`/` 等非版本化路径不受影响）
- 每个请求最终只能落到唯一一个版本
- 状态与元数据矛盾的配置在**加载阶段**即被拒绝
- 策略热更新与请求处理并发进行，任一时刻请求读到的都是自洽的单个策略快照

## 版本协商（Version Negotiation）

版本化路径（`/vN/...`）上的请求可通过以下四种来源表达期望版本，按**固定优先级**裁决：

| 优先级 | 来源 | 示例 |
| --- | --- | --- |
| 1（最高） | URL 路径 | `GET /v2/items` 或 `GET /version/2/items` |
| 2 | 请求头 | `X-API-Version: 2` |
| 3 | 媒体类型参数 | `Accept: application/json; version=2` |
| 4（最低） | 查询参数 | `?version=2` |

规则：

- **裁决来源**：按上表优先级，第一个**实际出现**的来源类别即为"裁决来源"，由它独立决定本次请求的版本结论；
- 裁决来源解析出唯一版本时，请求即按该版本裁决——更低优先级来源即使填了无法解析的值、或携带互相冲突的声明，也**不会**把请求判失败（这些非法声明不会被采纳，仅被忽略）；
- 只有当裁决来源自身无法给出唯一版本时，请求才被拒绝：
  - 取值无法解析（空值、非数字）→ `400 version_invalid`，即使更低优先级来源填了合法版本也不会回退采用；
  - 同一来源内部出现多个互相冲突的版本声明（如同一条 `Accept` 里两个不同的 `version=` 参数、重复的 `X-API-Version` 头、重复的 `?version=` 参数且取值不一致）→ `400 version_conflict`，**不会**静默挑选其中一个；取值重复但一致时不算冲突；
- 所有来源都未出现时返回 `400 version_required`，不隐式回退到默认版本；
- 版本在策略中不存在时返回 `404 version_unknown`；
- 三种无法判定的情形分别对应 `version_required` / `version_invalid` / `version_conflict` 三个互不相同的错误码，响应体 `error.details.source` 指出问题来源类别；
- 协商结果通过响应头 `X-API-Version` 与 `X-API-Version-Source` 透出，便于观测。

非版本化路径（`/`、`/gateway/*`、`/docs`、`/openapi.json`）不参与协商。

## 生命周期状态

每个版本处于且仅处于以下四种状态之一，状态由注入时钟按元数据时间推导：

| 状态 | 判定条件（`now` 为请求时间） | 行为 |
| --- | --- | --- |
| `available`（可用） | `now < deprecated_at`，且未公布 `sunset_at` | 正常响应，无提示头 |
| `planned_offline`（计划下线） | 已公布 `sunset_at` 但尚未到弃用时间 | 正常响应，带 `Sunset` 预警头 |
| `deprecated`（已弃用） | `deprecated_at <= now < sunset_at` | 响应体不变，带 `Deprecation` / `Sunset` / `Link` 头 |
| `offline`（已下线） | `now >= sunset_at` | `410 version_retired`，任何来源均不可命中 |

边界时刻语义（闭区间）：

- 请求时间**正好等于**弃用时间 → 按已弃用处理；
- 请求时间**正好等于**下线时间 → 按已下线处理。

时钟异常语义：

- 时钟回拨不会让已下线/已弃用的版本"重新变可用"——版本状态只允许单调向前（高水位标记）；
- **单调保护跨热更新生效**：高水位记录挂在 `PolicyManager` 上、按版本号索引，而非挂在策略快照上。某个版本一旦被判定为已下线，即使之后热更新策略（包括重新加载完全相同的内容）并把系统时间回拨到下线时间之前，发给该版本的请求仍返回 `410 version_retired` 与去向指引；重载失败同样不影响已生效的保护；
- **并发无窗口**：高水位的读取与更新在锁内完成，重载与请求并发进行时保护不会短暂失效；
- **记录规模有界**：高水位每个版本至多一条记录，规模只与版本数量相关，不随热更新次数增长，长期反复重载不会累积状态；
- 时区变化不影响判定——所有时间戳加载时统一归一化为 UTC。

## 标准响应头

命中已弃用版本的响应，响应体结构与可用版本完全一致，仅额外携带标准头：

- `Deprecation: @<unix-seconds>`（[RFC 8594](https://www.rfc-editor.org/rfc/rfc8594)）——弃用事实；
- `Sunset: <IMF-fixdate>`（[RFC 8594](https://www.rfc-editor.org/rfc/rfc8594)，HTTP-date）——下线时间，如 `Sat, 01 Jun 2027 00:00:00 GMT`；
- `Link: </v3/>; rel="successor-version"`（[RFC 5829](https://www.rfc-editor.org/rfc/rfc5829)）——替代版本指引。

计划下线版本的响应带 `Sunset` 预警头；已下线版本返回 `410` 并通过 `Link` 头（及响应体 `details.successor`）给出去向指引。

## 策略配置

策略为 JSON 文档，可通过文件（`GATEWAY_POLICY_PATH` 环境变量）加载，或由 `POST /gateway/reload` 在运行期热更新。

```json
{
  "min_deprecation_window_days": 30,
  "versions": [
    {
      "version": 2,
      "state": "deprecated",
      "introduced_at": "2024-01-01T00:00:00Z",
      "deprecated_at": "2025-01-01T00:00:00Z",
      "sunset_at": "2026-06-01T00:00:00Z",
      "successor": 3
    },
    {
      "version": 3,
      "state": "available",
      "introduced_at": "2025-01-01T00:00:00Z"
    }
  ]
}
```

配置项：

| 字段 | 说明 |
| --- | --- |
| `min_deprecation_window_days` | 弃用时间到下线时间的最短窗口（天），默认 30，必须为正数 |
| `versions` | 版本列表，非空，版本号为正整数且不重复 |
| `version` | 版本号 |
| `state` | 声明的生命周期：`available` / `planned_offline` / `deprecated` / `offline` |
| `introduced_at` | 引入时间（必填，ISO-8601 带时区） |
| `deprecated_at` | 弃用时间（ISO-8601 带时区） |
| `sunset_at` | 下线时间（ISO-8601 带时区） |
| `successor` | 替代版本号，必须指向策略中存在的其他版本 |

加载阶段拒绝的配置（`PolicyError`）：

- 状态与元数据矛盾：如 `state=available` 却已过弃用时间、`state=deprecated` 缺少 `deprecated_at`/`sunset_at`、`state=offline` 缺少 `sunset_at` 或下线时间在未来；
- 时间朴素（不带时区偏移）、时间顺序矛盾（如 `sunset_at` 不晚于 `deprecated_at`）；
- 弃用窗口短于 `min_deprecation_window_days`；
- `successor` 指向缺失版本或指向自身；
- 未知状态、重复版本号、空版本列表。

## 热更新（Hot Reload）

- `PolicyManager` 持有不可变策略快照，请求进入时读取一次并贯穿整个请求；
- 重载先构建并校验候选策略，通过后以**单次引用赋值**原子切换——重载失败不影响正在生效的策略；
- 在途请求持有的旧快照自然释放，慢请求不会阻塞重载，重载也不影响在途请求的自洽性；
- `POST /gateway/reload`：带 JSON 请求体时以其为新策略；否则重载 `GATEWAY_POLICY_PATH` 指向的文件。校验失败返回 `400 reload_failed`，当前策略保持不变。

## 可观测性

- `GET /gateway/versions`：各版本当前状态与元数据；
- `GET /gateway/stats`：按版本统计调用量，并按 `available` / `planned_offline` / `deprecated` / `offline` 分桶，用于识别仍停留在已弃用版本上的流量；
- 确定性：同一份策略与同一个可注入时钟下，把热更新也纳入序列后，相同的"请求 + 重载"交替序列仍得到逐条一致的版本结论与响应头（见 `tests/test_monotonic_protection.py` 的重放测试）。

## 错误码

| 状态码 | code | 含义 |
| --- | --- | --- |
| 400 | `version_required` | 未指定版本（所有来源均未出现） |
| 400 | `version_invalid` | 裁决来源的版本值无法解析 |
| 400 | `version_conflict` | 裁决来源内部出现互相冲突的版本声明 |
| 404 | `version_unknown` | 版本在策略中不存在 |
| 410 | `version_retired` | 版本已下线（给出去向指引） |
| 400 | `reload_failed` | 策略热更新校验失败 |

## 测试

```bash
pytest
```

在仓库根目录直接执行即可（`pyproject.toml` 已配置 `pythonpath`，无需设置环境变量或调整路径）。

覆盖：来源优先级与裁决来源语义、低优先级非法值不阻断、同一来源内部冲突拒绝、三类协商错误的区分、状态/元数据矛盾、窗口违规、边界时刻、时钟回拨与时区、已下线拒绝（四种来源）、时钟回拨叠加热更新后的单调保护、并发重载下无复活窗口、高水位记录规模有界、慢请求与重载互不阻塞、含重载的确定性重放、按版本统计。

关键场景与测试文件的对应关系：

| 场景 | 测试文件 |
| --- | --- |
| 低优先级来源填非法值/冲突值不阻断可判定请求 | `tests/test_resolver_strictness.py`、`tests/test_middleware.py` |
| 同一来源内部版本冲突被拒绝（`version_conflict`） | `tests/test_resolver_strictness.py`、`tests/test_middleware.py` |
| 时钟回拨 + 策略重载（含相同内容重载、失败重载）后已下线版本仍被拒绝 | `tests/test_monotonic_protection.py` |
| 并发重载与请求下的单调性（无复活窗口） | `tests/test_monotonic_protection.py` |
| 高水位记录规模只与版本数量相关 | `tests/test_monotonic_protection.py` |
| 含热更新的确定性重放 | `tests/test_monotonic_protection.py` |
