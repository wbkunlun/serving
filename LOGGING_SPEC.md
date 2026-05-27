# Knative Serving 日志运维规范

## 日志架构

日志基于 `knative.dev/pkg/logging` 实现，底层使用 `go.uber.org/zap`。所有组件通过 **ConfigMap 热更新**日志级别，无需重启 Pod。

## 日志初始化

有两种模式，取决于组件类型：

### 模式一：Controller 组件（自动初始化）

适用于 `sharedmain.Main` 启动的控制器（如 net-contour、webhook、hpa-autoscaler）：

```go
// cmd/controller/main.go
func main() {
    sharedmain.Main("net-contour-controller", contour.NewController)
    // sharedmain 内部自动完成：
    //   1. 读取 config-logging ConfigMap
    //   2. 创建 zap logger
    //   3. 注入到 context
    //   4. 监听 ConfigMap 变化动态更新级别
}
```

在 reconciler 中直接使用：

```go
func (r *Reconciler) ReconcileKind(ctx context.Context, ing *v1alpha1.Ingress) reconciler.Event {
    logger := logging.FromContext(ctx)
    logger = logger.With(
        zap.Int64("generation", ing.Generation),
        zap.String("resource-version", ing.ResourceVersion),
    )
    logger.Debug("reconciling ingress")
}
```

### 模式二：非 Controller 组件（手动初始化）

适用于自定义 main 入口（activator、autoscaler、queue-proxy）：

```go
// cmd/activator/main.go 或 cmd/autoscaler/main.go
const component = "activator" // 或 "autoscaler"

func main() {
    // 1. 从 config-logging ConfigMap 读取配置
    loggingConfig, err := sharedmain.GetLoggingConfig(ctx)

    // 2. 创建 logger，component 名称决定 config-logging 中的 loglevel.<name> 键
    logger, atomicLevel := logging.NewLoggerFromConfig(loggingConfig, component)

    // 3. 添加固定的上下文标签
    logger = logger.With(
        zap.String(logkey.ControllerType, component),
        zap.String(logkey.Pod, env.PodName),
    )

    // 4. 注入到 context
    ctx = logging.WithLogger(ctx, logger)

    // 5. 动态监听日志级别变更
    cmw.Watch(logging.ConfigMapName(),
        logging.UpdateLevelFromConfigMap(logger, atomicLevel, component))
}
```

## 组件名称与 Logger 映射

由 `sharedmain.Main("name", ...)` 的第一个参数或 `logging.NewLoggerFromConfig(config, "name")` 的第二参数决定：

| 组件 | Logger 名称 | 初始化方式 |
|------|------------|-----------|
| Controller | `controller` | `sharedmain.MainWithConfig(ctx, "controller", ...)` |
| Autoscaler | `autoscaler` | 手动 `NewLoggerFromConfig` |
| Queue Proxy | `queueproxy` | `cmd/queue` 手动初始化 |
| Webhook | `webhook` | `sharedmain.WebhookMainWithContext(ctx, "webhook", ...)` |
| Activator | `activator` | 手动 `NewLoggerFromConfig` |
| HPA Autoscaler | `hpaautoscaler` | `sharedmain.Main("hpaautoscaler", ...)` |
| Net-Contour | `net-contour-controller` | `sharedmain.Main("net-contour-controller", ...)` |
| Net-Gateway-API | `net-gateway-api-controller` | `sharedmain.Main("net-gateway-api-controller", ...)` |
| Net-Istio | `net-istio-controller` | `sharedmain.Main("net-istio-controller", ...)` |
| Net-Kourier | `net-kourier-controller` | `sharedmain.Main("net-kourier-controller", ...)` |

## ConfigMap 配置

ConfigMap `config-logging` 位于 `knative-serving` 命名空间：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: config-logging
  namespace: knative-serving
data:
  # zap logger 底层配置
  zap-logger-config: |
    {
      "level": "info",
      "development": false,
      "outputPaths": ["stdout"],
      "errorOutputPaths": ["stderr"],
      "encoding": "json",
      "encoderConfig": {
        "timeKey": "timestamp",
        "levelKey": "severity",
        "nameKey": "logger",
        "callerKey": "caller",
        "messageKey": "message",
        "stacktraceKey": "stacktrace",
        "levelEncoder": "",
        "timeEncoder": "iso8601",
        "durationEncoder": "",
        "callerEncoder": ""
      }
    }

  # 各组件日志级别覆盖
  loglevel.controller: "info"
  loglevel.autoscaler: "info"
  loglevel.queueproxy: "info"
  loglevel.webhook: "info"
  loglevel.activator: "info"
  loglevel.hpaautoscaler: "info"
  loglevel.net-contour-controller: "info"
  loglevel.net-gateway-api-controller: "info"
```

## 日志输出

**默认同时输出到 stdout 和数据文件。** stdout 供容器运行时采集（`kubectl logs`），文件用于持久化归档：

```json
{
  "outputPaths": ["stdout"],
  "errorOutputPaths": ["stderr"]
}
```

日志由容器运行时（containerd/docker）采集，通过 `kubectl logs` 查看。生产环境通过 DaemonSet（如 FluentBit、Filebeat）统一转发文件日志。如需改为纯 stdout 输出，设置 `LOG_ENABLE_FILE=false`。

## 文件日志与归档轮转

所有组件 **默认启用** 文件日志输出和归档轮转，无需额外配置。

### 实现位置

- **`pkg/logging/rotating_writer.go`** — 轮转写入器实现
- **`pkg/logging/sync_file_writer.go`** — 线程安全文件写入器
- **`pkg/logging/beijing_time_encoder.go`** — 时区编码器

### 启用方式

**Controller 组件**（controller、webhook、hpa-autoscaler、net-*）：通过 `sharedmain.SetupLoggerOrDie` 自动启用，由 `pkg/logging` 的 `init()` 注册钩子。

**非 Controller 组件**（activator、autoscaler、queue-proxy）：在各自 `main.go` 中创建 logger 后立即调用 `logging.EnableFileLogging(logger, configJSON, component)`。

### 配置环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_ENABLE_FILE` | `true` | 设为 `false` 禁用文件日志 |
| `LOG_DIR` | `/logs` | 日志文件输出目录 |
| `LOG_FILE_NAME` | `<component>.log` | 日志文件名（为空则以组件名命名） |
| `LOG_MAX_SIZE_MB` | `100` | 每个文件最大 100MB，超过则轮转 |
| `LOG_MAX_BACKUPS` | `720` | 保留最近 720 个归档文件 |
| `LOG_MAX_AGE_HOURS` | `24` | 归档文件保留 24 小时 |
| `LOG_COMPRESS` | `1` | 启用 gzip 压缩归档 |
| `LOG_TIMEZONE` | `Asia/Shanghai` | 日志时间戳时区 |

### 轮转机制

每次 `Write()` 检查当前文件大小，当 `currentSize + len(p) > maxSize` 时触发轮转：

1. 关闭当前文件
2. 重命名为 `base.log.<timestamp>`（格式 `2006-01-02T15-04-05`）
3. 如果 `LOG_COMPRESS=true`，gzip 压缩归档文件
4. 清理旧文件：保留最近 `maxBackups` 个，删除超过 `maxAge` 的

### 目录不可用时的降级

如果 `LOG_DIR` 目录创建失败，自动降级到系统临时目录 `os.TempDir()/knative-logs/`。

### 禁用文件日志

```bash
kubectl set env deployment/controller LOG_ENABLE_FILE=false
```

## 动态调整日志级别

修改 ConfigMap 后 **立即生效**，无需重启：

```bash
kubectl patch configmap/config-logging -n knative-serving \
  --type merge -p '{"data":{"loglevel.controller":"debug"}}'

# 验证
kubectl logs -n knative-serving deployment/controller --tail=50 | grep '"severity":"debug"'
```

## 结构化日志规则

### 错误必须使用 `*w` 方法

```go
// ✅ 正确
logger.Errorw("failed to reconcile", zap.Error(err))
logger.Warnw("failed to update service", zap.Error(err))

// ❌ 错误 - Infof 不接受 zap.Field
logger.Infof("failed to get service to determine cluster IP", zap.Error(err))
logger.Infof("failed to reconcile: %v", err)  // 丢失结构化信息
```

### `*f` 方法只用于格式化字符串

```go
// ✅ 正确
logger.Infof("processing %d items", len(items))
logger.Debugf("Found %d HTTP Proxies from older generations", len(oldGeneration))

// ❌ 错误
logger.Infof("some message", zap.Error(err))
```

### 相关字段用 `.With()` 聚合

```go
logger = logger.With(
    zap.String("resource", name),
    zap.String("namespace", namespace),
)
logger.Debug("processing")  // 自动带上 resource 和 namespace
```

### Sub-logger 用 `.Named()`

```go
logger.Named("config-store")
logger.Named("status-manager")
```

## 镜像构建规范

所有组件使用统一 Dockerfile 模板：

```dockerfile
FROM golang:1.25 AS builder
ARG component
WORKDIR /src
COPY . .
RUN GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o /ko-app ./cmd/${component}

FROM ghcr.io/wolfi-dev/static:alpine
ARG component
COPY --from=builder /ko-app /usr/local/bin/${component}
ENTRYPOINT ["/usr/local/bin/${component}"]
```

构建命令：

```bash
docker buildx build --platform linux/amd64 \
  --build-arg component=<component> \
  --tag <registry>:<component>-<version> \
  --push .
```

镜像标签格式：`<component>-<version>`（例如 `activator-v1.22.1`、`net-contour-controller-v1.22.1`）。
