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

## 接入桌面客户端

### 让 AI 自己装（推荐）

在你的 AI 客户端里打开本项目目录，粘贴这段，把 `<MCP_REPO>` 换成实际路径：

```text
读 <MCP_REPO>/docs/ai-install.md，按里面的步骤把 CalabiYau Map Engine MCP 注册到当前客户端。
仓库路径不对就先问我，不要猜。
```

agent 会自己判断它运行在哪个客户端、配置文件在哪、条目要不要 `type` 字段，合并写入并备份原文件，
然后让你重启客户端、引导浏览器配对。完整步骤和客户端配置对照表见
[docs/ai-install.md](docs/ai-install.md)；客户端读不了本地文件时，用该文件末尾的「完整版提示词」。

### 用命令装

```powershell
# 只检测并打印配置，不写文件
uv run python -m calabiyau_map_engine_mcp install

# 写入已安装客户端的配置文件（写入前自动备份）
uv run python -m calabiyau_map_engine_mcp install --apply

# 写入远程传输条目，供只支持 HTTP/SSE 的客户端使用
uv run python -m calabiyau_map_engine_mcp install --transport http --apply
```

`install` 会读取或生成凭据，打印 `bridge_url`、`pairing_token` 和各传输的配置片段，并检测
Claude Desktop、Cursor、Trae、VS Code 是否已安装。只有加 `--apply` 才真正写入；每次写入前把原文件备份为
`<文件名>.bak-<时间戳>`，只合并 `mcpServers` / `servers` 里的本服务条目，不动其他服务器。

默认写 stdio 条目，由客户端自己拉起服务，不需要预先常驻。改用 `--transport http` 时写入的是
`url` + `Authorization` 条目，必须先以 `--transport http` 常驻服务，否则客户端连不上。

`install` 会在终端显示 `pairing_token`。这是本机凭据，不要贴到公开渠道；需要撤销时执行
`reset-pairing` 轮换。

## 启动方式

```powershell
# 默认模式。由 MCP 客户端启动时使用，stdout 专供 MCP 消息。
uv run python -m calabiyau_map_engine_mcp --transport stdio

# Streamable HTTP。
uv run python -m calabiyau_map_engine_mcp --transport streamable-http

# 兼容只支持旧 HTTP+SSE 的客户端。
uv run python -m calabiyau_map_engine_mcp --transport sse

# 常驻服务：同时提供 Streamable HTTP 和旧 SSE，不接管 stdin。
uv run python -m calabiyau_map_engine_mcp --transport http

# STDIO、Streamable HTTP 和旧 SSE 同时启用。
uv run python -m calabiyau_map_engine_mcp --transport all
```

`http` 与 `all` 的区别只在 stdio：`all` 把 stdin EOF 当作退出信号，适合由 MCP 客户端拉起，
或在终端前台运行；`http` 完全不读 stdin，可以直接交给服务管理器、计划任务或开机自启。

也可以显式使用 `serve` 子命令。STDIO 模式同样会启动本机 WebSocket 桥接端口，但不启用 MCP 的 HTTP 端点。

| 路径 | 用途 | 启用模式 |
| --- | --- | --- |
| `/mcp` | Streamable HTTP | `streamable-http`、`http`、`all` |
| `/sse` | 旧 SSE 事件流入口 | `sse`、`http`、`all` |
| `/messages/` | 旧 SSE 的消息提交端点，由 SSE 客户端自动发现 | `sse`、`http`、`all` |
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
| `--transport` | `stdio` | `stdio`、`sse`、`streamable-http`、`http` 或 `all` |
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
| `list_items` | 分页查询可用物品，含 `connect_item_id`、`child_item_id` 连接契约 |
| `list_actors` | 分页查询当前地图中的 Actor |
| `list_links` | 分页查询连接关系：成对套件、有序编组和残留段 |
| `select_actors` | 按编辑器 Actor 标识设置选区 |
| `place_actor` | 在地图格子中放置物件 |
| `move_actors` | 按格子增量移动物件 |
| `delete_actors` | 删除指定物件 |
| `connect_actors` | 以给定顺序把子件挂到主件下，替换原有编组 |
| `disconnect_actors` | 断开成对套件或切断编组路径 |
| `undo`、`redo` | 使用现有编辑器撤销和重做历史 |

连接沿用编辑器自身的模型：主件的物品必须声明 `child_item_id`，子件物品必须与之相同，`child_actor_ids` 的顺序就是路径顺序。成对套件（`connect_item_id`）只能由编辑器成对放置，`connect_actors` 不接受已经是套件一半的成员，需要先 `disconnect_actors`。两种连接变更都进入编辑器的组合历史，因此 `undo`、`redo` 对它们同样有效。

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
