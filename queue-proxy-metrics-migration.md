# Queue-proxy / Activator 指标迁移指南（OpenCensus → OpenTelemetry）

> 适用版本：Knative Serving v1.22+（已迁移到 OpenTelemetry）
> 迁移提交：`c4b7c40a1`（activator）、`6dff36278`（queue-proxy）

本仓库已将 queue-proxy 和 activator 的指标体系从 **OpenCensus** 迁移到 **OpenTelemetry (OTel)**。指标名、单位、标签、聚合方式均有变化。本文档给出完整映射和 PromQL 改写参考。

---

## 1. 背景：为什么会变

| 维度 | 旧（OpenCensus） | 新（OpenTelemetry） |
|---|---|---|
| SDK | `go.opencensus.io` | `go.opentelemetry.io/otel` |
| Prometheus 命名规则 | contrib exporter，namespace 取 component 名（`queue_proxy_` / `activator_`） | OTel exporter + `UnderscoreEscapingWithSuffixes`（`.` → `_`，单位/total 后缀） |
| 标签 | OpenCensus tag | OTel attribute（`.` → `_`） |
| 指标结构 | 多个独立 measure（count + latency 分开） | 合并为更少的指标（延迟走 histogram，计数从 `_count` 取） |

**最稳妥的核对方式**（不同 OTel exporter 版本后缀规则有细微差异，以实际为准）：

```bash
kubectl exec <revision-pod> -n <ns> -c queue-proxy -- \
  curl -s localhost:9091/metrics | grep '^kn_'
```

---

## 2. 如何开启 queue-proxy 指标（Prometheus 拉取）

### 2.1 打开导出

```bash
kubectl patch configmap config-observability -n knative-serving \
  --type merge -p '{"data":{"request-metrics-protocol":"prometheus"}}'
```

`request-metrics-endpoint` 留空 → 自动监听 `:9091`。改完后 controller 会 GlobalResync 自动滚动所有 revision（30s ~ 几分钟），无需手动重启。

### 2.2 Prometheus 抓取（PodMonitor）

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: knative-queue-proxy-metrics
  namespace: knative-serving
  labels:
    release: kube-prometheus-stack   # 按实际 release 改
spec:
  namespaceSelector:
    any: true
  selector:
    matchLabels: {}
  podMetricsEndpoints:
    - port: http-usermetric          # 容器端口名 → 9091
      path: /metrics
      interval: 30s
      scrapeTimeout: 10s
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_label_serving_knative_dev_revision]
          action: keep
          regex: .+
```

原生 Prometheus（无 Operator）用 kubernetes_sd pod role，过滤 `serving_knative_dev_revision` 标签 + `http-usermetric` 端口名即可。

### 2.3 端口区分（重要）

| 端口 | 端口名 | 用途 |
|---|---|---|
| **9090** | `http-autometric` | autoscaler 内部用（并发/RPS，KPA 自动扩缩来源），**不要**给 dashboard |
| **9091** | `http-usermetric` | 用户请求指标（本文档的所有指标），需通过 config-observability 开启 |

> containerPort 9091 在 Deployment 里**总是声明**，但只有 `request-metrics-protocol: prometheus` 时才有 server 在 9091 监听。

---

## 3. 指标名映射

### 3.1 Queue-proxy（9091）

**新版指标（升级后实际看到的）：**

| OTel 名 | 类型 | Prometheus 名 |
|---|---|---|
| `kn.serving.invocation.duration` | Histogram (s) | `kn_serving_invocation_duration_seconds_bucket` / `_sum` / `_count` |
| `kn.serving.queue.depth` | Gauge | `kn_serving_queue_depth` |

**旧 → 新映射：**

| 旧（OpenCensus） | 新（OTel） | 语义变化 |
|---|---|---|
| `queue_proxy_request_count_total` | `kn_serving_invocation_duration_seconds_count` | 请求数从独立 counter 改用 histogram 的 `_count` |
| `queue_proxy_app_request_count_total` | `kn_serving_invocation_duration_seconds_count` | queue-proxy 侧与 user-container 侧**合并**为同一指标 |
| `queue_proxy_request_latencies_bucket` | `kn_serving_invocation_duration_seconds_bucket` | 延迟，**单位 ms → s**，分桶边界变化 |
| `queue_proxy_app_request_latencies_bucket` | `kn_serving_invocation_duration_seconds_bucket` | 同上，合并 |
| `queue_proxy_queue_depth` | `kn_serving_queue_depth` | 直接对应 |

> 注：你们环境历史上用的 `revision_app_request_count`（仓库标准名为 `queue_proxy_app_request_count`）属于同一概念，新版统一映射到 `kn_serving_invocation_duration_seconds`。

### 3.2 Activator

**新版指标：**

| OTel 名 | 类型 | Prometheus 名 |
|---|---|---|
| `kn.revision.request.concurrency` | Gauge | `kn_revision_request_concurrency` |
| `kn.revision.request.queued` | UpDownCounter | `kn_revision_request_queued` |
| `kn.revision.request.active` | UpDownCounter | `kn_revision_request_active` |
| `kn.activator.stats.conn.reachable` | Gauge | `kn_activator_stats_conn_reachable` |
| `kn.activator.stats.conn.errors` | Counter | `kn_activator_stats_conn_errors_total` |

**旧 → 新映射：**

| 旧（OpenCensus） | 新（OTel） | 语义变化 |
|---|---|---|
| `activator_request_concurrency` | `kn_revision_request_concurrency` | 直接对应 |
| `activator_request_count_total` | `kn_revision_request_active` + `kn_revision_request_queued` | 重构为「正在代理 / 排队中」 |
| `activator_request_latencies_bucket` | `kn_serving_invocation_duration_seconds_bucket` | activator 侧延迟已移除，改用 queue-proxy 的端到端延迟 |

> Activator 的指标在 activator 进程导出（控制面 `metrics-protocol` 配置），不是 9091。

---

## 4. 标签映射（PromQL label 改写）

| 旧 label（OpenCensus tag） | 新 label（OTel attribute） | 说明 |
|---|---|---|
| `namespace_name` | `k8s_namespace_name` | **必须改** |
| `pod_name` | `k8s_pod_name`（另有 `service_instance_id`） | 改 |
| `container_name` | `k8s_container_name`（恒为 `queue-proxy`） | 改 |
| `revision_name` | `kn_revision_name` | 改（另有 `service_name` 兜底 = revision 名） |
| `configuration_name` | `kn_configuration_name` | 改 |
| `service_name` | `service_name` | **保持不变**（OTel `service.name` 转换后 label 名相同，ksvc 场景值仍为 ksvc 名） |
| — | `kn_service_name` | 仅从 ksvc 创建的 revision 有，更精确，可选替代 `service_name` |
| `response_code` | `http_response_status_code` | **必须改**（值为 `200` 等具体码字符串） |
| `response_code_class` | **已移除** | 新版无 class 聚合，需用 status_code 正则（如 `=~"2.."`）替代 |
| `route_tag` | `kn_route_tag` | 改 |

> ⚠️ `response_code_class` 被移除是高频踩坑点。原来 `response_code_class="2xx"` → 新版 `http_response_status_code=~"2.."`。

---

## 5. PromQL 改写参考

### 5.1 应耗时 P95

**旧：**
```promql
histogram_quantile(0.95, sum(rate(activator_request_latencies_bucket{namespace_name="$namespace",service_name=~"$ksvc.*",response_code_class="2xx"}[5m])) by (le))
```

**新：**
```promql
histogram_quantile(0.95, sum(rate(kn_serving_invocation_duration_seconds_bucket{k8s_namespace_name="$namespace",service_name=~"$ksvc.*",http_response_status_code=~"2.."}[5m])) by (le))
```

> P95 数值单位现在是**秒**（原来是 ms），数值会 ÷1000，阈值（如 < 0.5s）按秒写。

### 5.2 TPS（吞吐）

**旧：**
```promql
round(sum(rate(revision_app_request_count{namespace_name="$namespace",service_name=~"$ksvc.*"}[5m])), 0.001)
```

**新：**
```promql
round(sum(rate(kn_serving_invocation_duration_seconds_count{k8s_namespace_name="$namespace",service_name=~"$ksvc.*"}[5m])), 0.001)
```

> histogram 的 `_count` 单调递增，`rate()` 用法不变。

### 5.3 状态码频次

**旧（按 class 分组）：**
```promql
round(sum(rate(revision_app_request_count{namespace_name="$namespace",service_name=~"$ksvc.*"}[5m])) by (response_code_class), 0.001)
```

**新（按具体状态码分组）：**
```promql
round(sum(rate(kn_serving_invocation_duration_seconds_count{k8s_namespace_name="$namespace",service_name=~"$ksvc.*"}[5m])) by (http_response_status_code), 0.001)
```

**新（可选：保持 class 分组，2xx/4xx/5xx）：**
```promql
sum by (status_class) (
  label_replace(
    rate(kn_serving_invocation_duration_seconds_count{k8s_namespace_name="$namespace",service_name=~"$ksvc.*"}[5m]),
    "status_class", "${1}xx", "http_response_status_code", "^(\\d)\\d\\d$"
  )
)
```

### 5.4 请求成功率

**旧：**
```promql
sum(rate(revision_app_request_count{namespace_name="$namespace",response_code_class!="5xx",service_name=~"$ksvc.*"}[5m]))
/
sum(rate(revision_app_request_count{service_name=~"$ksvc.*"}[5m]))
```

**新：**
```promql
sum(rate(kn_serving_invocation_duration_seconds_count{k8s_namespace_name="$namespace",service_name=~"$ksvc.*",http_response_status_code!~"5.."}[5m]))
/
sum(rate(kn_serving_invocation_duration_seconds_count{k8s_namespace_name="$namespace",service_name=~"$ksvc.*"}[5m]))
```

> 注意：原版分母漏了 `namespace_name` 过滤（会把其它 namespace 的请求也算进分母），新版已补上。

---

## 6. 关键注意事项

1. **单位 ms → s**：所有延迟类指标现在是秒。P95/avg 数值 ÷1000，告警阈值改成秒。
2. **分桶边界变了**：新 histogram 显式边界为 `{0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10}` 秒（见 `pkg/queue/request_metric.go`）。p99/p95 计算结果与旧版不同。
3. **计数合并**：旧版 `request_count`（queue-proxy 侧）与 `app_request_count`（user-container 侧）是两个指标，新版合并为一个 `kn.serving.invocation.duration`。原来靠两者差值衡量 queue-proxy 自身开销的用法失效。
4. **`response_code_class` 被移除**：所有依赖 `2xx` / `4xx` / `5xx` 的过滤要改成 status_code 正则。
5. **`service_name` 保持不变**：OTel `service.name` 转换后 label 名仍为 `service_name`，ksvc 场景值不变，原 query 的 `service_name=~"$ksvc.*"` 可原样保留。
6. **生效延迟**：改 config-observability 后，controller GlobalResync 自动滚动 revision，单个 30s~2min，revision 多时 5~15min。要立刻全量生效可手动 `kubectl rollout restart deployment -A -l 'serving.knative.dev_revision'`。
7. **以实际 scrape 为准**：UpDownCounter 是否带 `_total` 后缀在不同 OTel exporter 版本可能有差异，务必 `curl localhost:9091/metrics | grep kn_` 核对。

---

## 7. 验证清单

```bash
# 1. 配置已生效（期望输出 "prometheus"）
kubectl get pod <pod> -n <ns> \
  -o jsonpath='{.spec.containers[?(@.name=="queue-proxy")].env[?(@.name=="OBSERVABILITY_CONFIG")].value}' \
  | jq '.requestMetrics.protocol'

# 2. 9091 已监听，能看到 kn_ 开头指标
kubectl exec <pod> -n <ns> -c queue-proxy -- \
  curl -s localhost:9091/metrics | grep '^kn_'

# 3. Prometheus 已抓到（在 Prometheus UI 查询）
#    up{job="knative-queue-proxy"} 或 kn_serving_invocation_duration_seconds_count
```
