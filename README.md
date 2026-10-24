# CalabiYau Map Engine MCP

通过本机 MCP 服务连接 CalabiYau Map Engine 浏览器编辑器。

支持 STDIO、Streamable HTTP 和旧 HTTP+SSE。

![MCP 架构：STDIO、SSE、Streamable HTTP 与本地浏览器 WebSocket 桥接](docs/images/mcp-architecture.png)

## 环境

- Python 3.11 或更新版本。
- [uv](https://docs.astral.sh/uv/)。
- 支持本地网络访问授权和 WebSocket 的桌面浏览器，推荐更新后的 Chrome 或 Edge。

Map Engine 站点：`https://klbqmap.miyo.wiki`

## 安装与配对

在本项目目录执行：

```powershell
uv sync --python 3.11
uv run python -m calabiyau_map_engine_mcp pair
```

`pair` 在当前用户的配置目录读取或首次生成凭据，并在终端显示 JSON：

| 字段 | 用途 |
| --- | --- |
| `bridge_url` | 浏览器 WebSocket 地址，默认为 `ws://127.0.0.1:8765/bridge` |
| `mcp_url` | Streamable HTTP 地址，默认为 `http://127.0.0.1:8765/mcp` |
| `sse_url` | 旧 SSE 地址，默认为 `http://127.0.0.1:8765/sse` |
| `pairing_token` | 仅填入浏览器的配对凭据 |
| `http_token` | 仅用于 MCP HTTP 客户端的 Bearer 认证 |
| `allowed_origins` | 服务允许连接的网页来源 |

两个 token 相互独立。再次执行 `pair` 不会轮换已保存的凭据。

启动服务后，在更新后的 Map_Engine 中打开“设置 → MCP”，填写 `bridge_url` 和 `pairing_token`，点击“连接并配对”。首次连接可能出现浏览器的本地网络访问授权提示，需要用户手动允许。

## 启动方式

```powershell
# 默认模式。由 MCP 客户端启动时使用，stdout 专供 MCP 消息。
uv run python -m calabiyau_map_engine_mcp --transport stdio

# Streamable HTTP。
uv run python -m calabiyau_map_engine_mcp --transport streamable-http

# 兼容只支持旧 HTTP+SSE 的客户端。
uv run python -m calabiyau_map_engine_mcp --transport sse

# STDIO、Streamable HTTP 和旧 SSE 同时启用。
uv run python -m calabiyau_map_engine_mcp --transport all
```

也可以显式使用 `serve` 子命令。STDIO 模式同样会启动本机 WebSocket 桥接端口，但不启用 MCP 的 HTTP 端点。

| 路径 | 用途 | 启用模式 |
| --- | --- | --- |
| `/mcp` | Streamable HTTP | `streamable-http`、`all` |
| `/sse` | 旧 SSE 事件流入口 | `sse`、`all` |
| `/messages/` | 旧 SSE 的消息提交端点，由 SSE 客户端自动发现 | `sse`、`all` |
| `/bridge` | 浏览器 WebSocket 桥接 | 全部模式 |
| `/healthz` | 最小健康检查 | 全部模式 |

HTTP 客户端应在 `/mcp`，或旧 SSE 的事件流和消息提交请求中携带：

```http
Authorization: Bearer <http_token>
```

`/sse` 是兼容旧客户端的独立传输，不是 `/mcp` 的别名。`/mcp` 根据 MCP 协议和请求内容返回 JSON 或 SSE；

`stdio` 和 `all` 将标准输入 EOF 视为进程退出信号，随后关闭桥接和 HTTP 监听。希望服务在关闭某个 MCP 客户端后独立常驻时，使用 `streamable-http` 或 `sse` 模式。

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--transport` | `stdio` | `stdio`、`sse`、`streamable-http` 或 `all` |
| `--port` | `8765` | MCP HTTP 和浏览器桥接共用的本机端口 |
| `--allowed-origin` | 见下文 | 可重复传入，追加精确的网页来源 |
| `--config-dir` | 当前用户的应用配置目录 | 自定义凭据存储目录 |
| `--request-timeout` | `30` | 浏览器请求的超时秒数 |
| `--log-level` | 服务默认日志级别 | 日志写入 stderr |

服务始终监听 `127.0.0.1`，没有开放公网或局域网监听的 `--host` 参数。默认网页来源：

```text
http://127.0.0.1:5173
http://localhost:5173
https://klbqmap.miyo.wiki
```

自定义开发端口时，增加对应的来源，例如：

```powershell
uv run python -m calabiyau_map_engine_mcp --transport streamable-http --allowed-origin http://127.0.0.1:5174
```

`--config-dir` 也可通过 `CALABIYAU_MCP_CONFIG_DIR` 环境变量配置。`pair`、`serve` 和 MCP 客户端必须使用相同目录，否则会读取到不同凭据。

### 客户端配置

参见 [examples/mcp-clients.json](examples/mcp-clients.json)。该文件展示三种连接方式，使用的是占位路径和占位 token，需按实际客户端支持的配置格式选择条目。

示例中的 STDIO 条目使用 `all`，因此后续 HTTP 与 SSE 条目可连接它启动的同一个进程。只需要一个 STDIO 客户端时，将该条目的 `all` 改为 `stdio`。

如果 MCP 客户端找不到 `uv`，将 `command` 改为本机 `uv` 的绝对路径。`--directory` 必须指向本项目的绝对路径。不同客户端可能使用不同的远程传输类型名称，地址和 Bearer header 保持不变。

## 地图工具

先调用 `list_sessions` 获得 `browser_session_id`，再针对选定浏览器调用 `get_state`。同一服务支持多个浏览器标签页。

| 工具 | 行为 |
| --- | --- |
| `list_sessions` | 列出已连接并完成配对的浏览器会话 |
| `get_state` | 返回当前地图和编辑状态、地图会话及修订号 |
| `list_items` | 分页查询可用物品 |
| `list_actors` | 分页查询当前地图中的 Actor |
| `select_actors` | 按编辑器 Actor 标识设置选区 |
| `place_actor` | 在地图格子中放置物件 |
| `move_actors` | 按格子增量移动物件 |
| `delete_actors` | 删除指定物件 |
| `undo`、`redo` | 使用现有编辑器撤销和重做历史 |

具体桥接消息和错误约定参见 [docs/protocol.md](docs/protocol.md)。

## 凭据与撤销配对

浏览器中的“忘记配对”只清除该网页来源下本浏览器保存的连接设置，不轮换服务端 token。

若凭据泄露或需要撤销全部已配对页面：

1. 停止正在运行的服务。
2. 在相同配置目录下执行 `uv run python -m calabiyau_map_engine_mcp reset-pairing`。
3. 再执行 `pair` 获取新凭据，更新 HTTP 客户端并重新配对浏览器。
4. 重新启动服务。

## HTTPS 页面连接本机服务

网页仍连接本机的 `http://127.0.0.1:8765` 和 `ws://127.0.0.1:8765`。浏览器可能要求用户授予本地网络访问权限，权限被拒绝时，应在浏览器站点设置中恢复权限，再手动连接。

站点若配置 Content Security Policy，`connect-src` 需要明确允许本机 HTTP 健康检查地址和 WebSocket 地址。使用自定义端口时，也需要相应更新 CSP 和连接设置。

浏览器策略、组织策略或网页 CSP 可能阻止连接；此时先通过本地开发页面诊断。
