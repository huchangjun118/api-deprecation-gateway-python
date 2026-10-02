# API 版本协商与弃用生命周期

本仓库在原有 FastAPI 应用之外增加了一层独立的**版本协商 + 弃用生命周期管理**
能力（包 `version_gateway`）。它以 ASGI 中间件形式接入，不改变任何既有接口
的响应体与状态码：对可用版本，请求照旧透传；对已弃用版本，仅追加标准提示头；
对已下线版本，返回带迁移指引的明确错误。

## 1. 版本标识来源与优先级

客户端可以用四种方式表达期望版本。多个来源**同时出现**时，按以下固定优先级
裁决，**最高优先级来源一票决定**，不会因为来源之间"冲突"而报错，也不会猜测：

| 优先级 | 来源 | 默认表达 | 配置项 |
| --- | --- | --- | --- |
| 1（最高） | 请求路径 | `/v2/items` | `version_path_regex`（须含 `(?P<version>...)` 捕获组，默认 `/v(?P<version>\d+)`） |
| 2 | 请求头 | `X-Api-Version: 2` | `header_name` |
| 3 | 查询参数 | `?api_version=2` | `query_param` |
| 4（最低） | 媒体类型参数 | `Accept: application/json; version=2` | `accept_version_param` |

裁决规则：

- 四个来源都缺省时，使用策略的 `default_version`；若未配置默认版本，返回
  `400 version_required`（响应体给出当前可用版本列表）。
- 最高优先级来源**显式存在但值为空/空白**，返回 `400 malformed_version`，
  不会静默降级到低优先级来源（降级等于猜测客户端意图）。
- 同一个 `Accept` 头里出现两个**不同**的版本值（如
  `application/json;version=1, application/json;version=2`），返回
  `400 ambiguous_version`。
- 版本号是非透明令牌：来源只负责取值，版本是否存在由解析阶段判定。未知版本
  返回 `404 unknown_version`。

错误码（均可被客户端区分）：

| HTTP | `error.code` | 含义 |
| --- | --- | --- |
| 400 | `version_required` | 未携带版本且无默认版本 |
| 400 | `malformed_version` | 显式版本值为空或格式非法 |
| 400 | `ambiguous_version` | 同一媒体类型头内出现多个不同版本 |
| 404 | `unknown_version` | 版本标识不在策略中 |
| 404 | `version_not_introduced` | 版本存在但尚未到引入时刻 |
| 410 | `version_retired` | 版本已下线（附替代版本与迁移链接） |

## 2. 版本生命周期状态与元数据

每个版本（`VersionInfo`）处于四种状态之一：

| 状态 | 值 | 语义 |
| --- | --- | --- |
| 可用 | `available` | 完全支持，不输出任何弃用提示 |
| 已弃用 | `deprecated` | 仍可服务；响应带 `Deprecation`（及替代版本），无确定下线时间 |
| 计划下线 | `planned-retirement` | 已弃用且下线时间已公布；额外带 `Sunset` |
| 已下线 | `retired` | 永久不可用；任何来源的请求一律 `410` 拒绝 |

元数据字段：

- `introduced_at`：引入时间（必填）
- `deprecated_at`：弃用时间
- `sunset_at`：下线时间
- `successor`：替代版本 id

### 加载阶段校验（矛盾配置不会生效）

`PolicyStore.reload()` / 构造时会先整体校验，**任何矛盾都抛出
`PolicyValidationError`，旧策略继续生效**：

- 时间戳必须是**带时区**的 `datetime`（不猜测时区）；
- `available` 不得设置 `deprecated_at` / `sunset_at` / `successor`；
- `deprecated`/`planned-retirement`/`retired` 必须有 `deprecated_at` 和
  `successor`；`planned-retirement`/`retired` 必须有 `sunset_at`；
- `introduced_at <= deprecated_at <= sunset_at`；
- 弃用窗口 `sunset_at - deprecated_at` 必须 **≥**
  `minimum_deprecation_window`（默认 30 天，可在策略中配置）；
- `successor` 必须是策略内已定义的版本、不能指向自身、不能指向已下线版本；
- `default_version` 必须存在；路径正则必须可编译且含 `version` 捕获组；
  `version_url_template` 必须包含 `{version}` 占位符。

## 3. 边界时刻与时钟异常语义

有效状态由"元数据 + 请求时刻"推导，边界采用**闭区间**：

- `now == deprecated_at`：**已经**处于已弃用/计划下线；
- `now == sunset_at`：**已经**下线，请求被 `410` 拒绝；
- 声明为 `retired` 的版本对任何时刻（含理论上的过去）都是下线状态。

`PolicyStore` 维护一个**跨请求、跨重载单调递增的观测时间高水位**：
决策时刻取 `max(clock.now(), 高水位)`。因此：

- 时钟回拨**不能**让已下线版本复活；
- 不同时区只是同一瞬间的不同表示（内部统一转 UTC 比较）；
- 高水位在策略热更新之后仍然保留。

时钟通过 `Clock` 协议注入（`SystemClock` / `FixedClock`）。在同一份策略与
同一个（可注入）时钟下，相同请求序列必然得到逐条一致的版本结论与响应头。

## 4. 响应头与响应体

对命中**已弃用 / 计划下线**版本的成功响应，中间件只追加头，**响应体字节原样
流式透传**，既有客户端解析不会受影响：

- `Deprecation: @Sun, 01 Feb 2026 00:00:00 GMT`（RFC 8594，弃用事实）
- `Sunset: Tue, 03 Mar 2026 00:00:00 GMT`（RFC 8594，下线时间）
- `Link: <https://api.example.com/v2/>; rel="successor-version"; anchor="#version-2"`
- `X-Api-Successor-Version: 2`（便于机器直接读取替代版本）

命中**已下线**版本时返回 `410`，响应体示例：

```json
{
  "error": {
    "code": "version_retired",
    "message": "API version '1' has been retired; migrate to version '2'",
    "successor_version": "2",
    "successor_url": "https://api.example.com/v2/"
  }
}
```

同时带 `Deprecation` / `Sunset` / `Link` / `X-Api-Successor-Version`。
没有配置 `version_url_template` 时，会尽力把请求路径中的版本段重写成替代版本。

## 5. 运行期热更新与并发

- `reload(policy)` **先完整校验再原子替换**单个引用；校验失败时生效中的
  策略完全不受影响。
- 每个请求只在取快照时短暂持锁，之后全程读取不可变 `Policy`：慢请求不会
  阻塞重载，重载也不会让请求看到"半截策略"。
- 测试见 `tests/test_store.py`、`tests/test_concurrent_reload.py`
  （含重载与请求并发交织场景）。

## 6. 可观测性

`VersionMetrics` 以线程安全方式按版本 × 有效状态计数：

- 桶：`available` / `deprecated` / `planned-retirement` / `retired` / `rejected`
- `snapshot()`：深拷贝全部计数；
- `deprecated_versions()`：仍在接收已弃用流量的版本及其调用量，用于识别
  尚未迁移的客户端；
- 被拒绝的请求（未知/缺失/已下线）也按版本记入 `rejected`。

## 7. 配置项一览（`Policy`）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `versions` | `{}` | `version id -> VersionInfo`，至少一个 |
| `default_version` | `None` | 无版本标识时使用；`None` 则拒绝无版本请求 |
| `minimum_deprecation_window` | `timedelta(days=30)` | 弃用→下线最短窗口 |
| `header_name` | `X-Api-Version` | 版本请求头名 |
| `query_param` | `api_version` | 版本查询参数名 |
| `accept_version_param` | `version` | 媒体类型版本参数名 |
| `version_path_regex` | `/v(?P<version>\d+)` | 路径版本提取正则 |
| `version_url_template` | `None` | 替代版本 URL 模板，含 `{version}` |

## 8. 接入方式

```python
from fastapi import FastAPI
from version_gateway import Policy, PolicyStore, VersionInfo, VersionState
from version_gateway.middleware import install

app = FastAPI()
store, metrics = install(app, policy)   # policy 或 PolicyStore 均可
# 运行期：store.reload(new_policy) 原子热更新
# 观测：metrics.snapshot() / metrics.deprecated_versions()
```

运行测试：`uv run pytest -q`。
