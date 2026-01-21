# 504 Timeout 问题诊断和解决方案

## 问题摘要

当看到 `receive_response_body.failed exception=CancelledError` 日志时，通常伴随着用户侧的 504 Gateway Timeout 错误。

**根本原因**：请求链路中某个组件的超时配置比 claude-code-proxy 更短，导致上游组件提前断开连接。

## 请求链路分析

```
用户 -> TLB -> Nginx -> claude-code-proxy -> OpenAI API
        ^30s?  ^60s?    ^600s (READ_TIMEOUT)
```

## 超时配置检查清单

### 1. 检查 claude-code-proxy 的超时配置

当前配置位于 `.env` 文件或环境变量：

```bash
# 查看当前配置
grep TIMEOUT .env

# 推荐配置
CONNECT_TIMEOUT=10      # 建立连接超时（秒）
READ_TIMEOUT=600        # 读取响应超时（秒）- 重要！
WRITE_TIMEOUT=10        # 发送请求超时（秒）
POOL_TIMEOUT=10         # 连接池获取超时（秒）
```

**关键**：`READ_TIMEOUT=600` (10分钟) 允许长时间的流式响应。

### 2. 检查 Nginx 超时配置

找到 Nginx 配置文件（通常在 `/etc/nginx/nginx.conf` 或 `/etc/nginx/sites-enabled/*`）：

```bash
# 查找 Nginx 配置
sudo find /etc/nginx -name "*.conf" -exec grep -l "claude-code-proxy" {} \;
```

**需要检查的配置项**：

```nginx
location / {
    proxy_pass http://claude-code-proxy;

    # 关键超时配置 - 必须 >= claude-code-proxy 的 READ_TIMEOUT
    proxy_read_timeout 600s;        # 读取上游响应超时
    proxy_connect_timeout 10s;      # 连接上游超时
    proxy_send_timeout 10s;         # 发送请求到上游超时

    # 可选但推荐
    proxy_buffering off;            # 禁用缓冲，立即转发流式响应
    proxy_cache off;                # 禁用缓存

    # 保持连接活跃
    proxy_http_version 1.1;
    proxy_set_header Connection "";
}
```

**修改 Nginx 配置后**：

```bash
# 测试配置
sudo nginx -t

# 重载配置
sudo nginx -s reload
```

### 3. 检查 TLB (Tengine Load Balancer) 配置

联系运维团队检查以下配置：

- **upstream_read_timeout**：建议设置为 600s 或更长
- **proxy_read_timeout**：建议设置为 600s 或更长
- **keepalive_timeout**：建议设置为 65s 以上

### 4. 检查防火墙和中间代理

某些防火墙或代理可能有默认的连接超时限制：

```bash
# 检查 iptables 连接超时
sudo iptables -L -n -v | grep -i timeout

# 检查系统 TCP keepalive 设置
sysctl net.ipv4.tcp_keepalive_time
sysctl net.ipv4.tcp_keepalive_intvl
sysctl net.ipv4.tcp_keepalive_probes
```

## 诊断方法

### 方法 1：查看改进后的日志

更新后的代码会记录详细的时间信息：

```
[request_id] First byte received after X.XXs
[request_id] Client disconnected after Y.YYs (chunks received: N, first byte: true). Possible upstream timeout.
[request_id] Streaming completed successfully in Z.ZZs (chunks: M, TTFB: X.XXs)
```

**关键指标**：
- **TTFB (Time To First Byte)**：如果 > 30s，可能触发某些超时
- **断开时间**：如果接近 60s/90s/120s，说明是固定的超时配置

### 方法 2：使用 curl 测试超时

```bash
# 测试到 claude-code-proxy 的连接
curl -v -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-api-key" \
  -d '{"model":"claude-3-5-sonnet-20241022","max_tokens":1024,"messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  --max-time 700

# 如果在 60s/90s 断开，说明中间有超时限制
```

### 方法 3：抓包分析

```bash
# 在服务器上抓包
sudo tcpdump -i any -s 0 -w /tmp/timeout.pcap 'port 8000 or port 80 or port 443'

# 分析哪一端先发送 FIN/RST 包
```

## 解决方案

### 方案 A：统一超时配置（推荐）

**目标**：让链路中所有组件的超时配置保持一致（建议 600s）

1. **TLB 配置** → 600s
2. **Nginx 配置** → 600s (`proxy_read_timeout 600s;`)
3. **claude-code-proxy 配置** → 600s (`READ_TIMEOUT=600`)

### 方案 B：降低 claude-code-proxy 超时（备选）

如果无法修改上游配置，只能降低 claude-code-proxy 的超时：

```bash
# .env 文件
READ_TIMEOUT=60  # 匹配 Nginx/TLB 的超时时间
```

**缺点**：会影响长时间流式响应的处理。

### 方案 C：添加心跳机制（已实现）

代码已经在流式响应中发送 ping 事件（`response_converter.py:246`）：

```python
yield f"event: {Constants.EVENT_PING}\ndata: {json.dumps({'type': Constants.EVENT_PING}, ensure_ascii=False)}\n\n"
```

这可以防止某些代理认为连接空闲而断开。

**优化建议**：定期发送 ping（每 15-30 秒）。

## 常见超时值参考

| 组件 | 默认值 | 推荐值 | 说明 |
|------|--------|--------|------|
| TLB | 30-60s | 600s | 负载均衡器通常较短 |
| Nginx | 60s | 600s | `proxy_read_timeout` |
| claude-code-proxy | 600s | 600s | `READ_TIMEOUT` |
| OpenAI API | N/A | N/A | 由 OpenAI 控制 |

## 验证修复效果

### 1. 重启服务

```bash
# 重启 claude-code-proxy
systemctl restart claude-code-proxy
# 或
docker restart claude-code-proxy

# 重载 Nginx
sudo nginx -s reload
```

### 2. 测试长时间请求

```bash
# 发送一个会产生长响应的请求
curl -X POST http://your-domain/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-api-key" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 4096,
    "messages": [
      {
        "role": "user",
        "content": "Please write a very detailed explanation of quantum computing (at least 3000 words)."
      }
    ],
    "stream": true
  }'
```

### 3. 监控日志

```bash
# 实时查看日志
tail -f logs/app.log | grep -E "First byte|disconnected|completed"
```

**成功的标志**：
- 看到 `Streaming completed successfully` 日志
- 没有 `Client disconnected` 警告
- 总时间可以超过之前的超时限制

## 应急排查命令

```bash
# 1. 检查当前活跃连接
netstat -an | grep :8000 | grep ESTABLISHED

# 2. 检查最近的超时错误
grep -i "disconnected\|timeout\|cancelled" logs/app.log | tail -20

# 3. 统计超时发生的时间分布
grep "Client disconnected after" logs/app.log | \
  sed -E 's/.*after ([0-9.]+)s.*/\1/' | \
  sort -n | uniq -c

# 4. 查看 Nginx 错误日志
sudo tail -f /var/log/nginx/error.log | grep timeout
```

## 联系和上报

如果问题持续存在，请收集以下信息上报：

1. **日志片段**：包含时间戳、request_id、断开时间的完整日志
2. **配置信息**：Nginx、TLB、claude-code-proxy 的超时配置
3. **网络拓扑**：请求链路中所有组件及其超时配置
4. **复现步骤**：能稳定复现问题的请求示例

---

**最后更新**：2026-01-21
**相关 commit**：`fix: 添加详细的超时诊断日志`
