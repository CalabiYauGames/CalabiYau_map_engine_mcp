# 让 AI 自己安装并注册 MCP

本文件有两个读者：上面几段是给人看的，`## 给 AI 的指令` 之后是给 agent 看的。

> **配置完成后**，查看 **[modeling-guide.md](modeling-guide.md)** 了解如何让 AI 通过 MCP 工具建模。

## 给人看：怎么用

1. 在你的 AI 客户端里，**打开 `CalabiYau_map_engine_mcp` 这个目录**（Trae / Cursor / Claude Code 都可以），新建一个会话。
2. 粘贴下面这段短提示词，把 `<MCP_REPO>` 换成实际路径：

```text
读 <MCP_REPO>/docs/ai-install.md，按里面的步骤把 CalabiYau Map Engine MCP 注册到当前客户端。
仓库路径不对就先问我，不要猜。
```

3. agent 写完配置后会让你重启客户端。**必须重启**——MCP 配置只在启动时读取，它没法在本次会话里加载自己刚写的配置。
4. 重启后回来说一句"验证一下"，它会调 `list_sessions` 确认服务通了，然后带你做浏览器配对。

如果客户端读不了本地文件，用文末的「完整版提示词」，整段贴进去，自包含。

## 为什么不做一键安装脚本

因为每个客户端的配置路径、键名、条目形状都不一样，而且这些路径会随版本变。脚本一旦写死就会在别人机器上静默失效。
让 agent 现场判断"我在哪个客户端里、配置文件在哪、要不要 `type` 字段"，比维护一张硬编码表更耐用——出问题时它还能告诉你错在哪。

---

## 给 AI 的指令

你要把本机的 CalabiYau Map Engine MCP 服务注册进**你当前运行的这个客户端**，然后引导用户完成浏览器配对。
只做安装，不要动地图。

### 0. 确认仓库

仓库根目录就是本文件所在目录的上一级。确认这两样存在：

- `<MCP_REPO>/pyproject.toml`
- `<MCP_REPO>/.venv/Scripts/python.exe`（Windows）或 `<MCP_REPO>/.venv/bin/python`（macOS/Linux）

`.venv` 不存在时，先在仓库根目录执行 `uv sync --python 3.11`，再继续。
**不要**用系统 python，也不要 `uv run`——客户端拉起服务时不会继承你的 shell 环境。

### 1. 判断你在哪个客户端里

优先用你能确定的自身产品信息；不确定就直接问用户，**不要猜**。

### 2. 找到该客户端的 MCP 配置文件

| 客户端 | 配置文件 | 顶层键 | 条目要求 |
| --- | --- | --- | --- |
| Trae / Trae CN | `%APPDATA%\Trae CN\User\mcp.json`（国际版是 `%APPDATA%\Trae\User\mcp.json`） | `mcpServers` | 无 `type` |
| Cursor | `%USERPROFILE%\.cursor\mcp.json`，或项目内 `.cursor/mcp.json` | `mcpServers` | 无 `type` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` | 无 `type`，需完全退出应用 |
| Claude Code | 项目根 `.mcp.json`，或用户级 `~/.claude.json` | `mcpServers` | 无 `type` |
| VS Code / Copilot | 项目内 `.vscode/mcp.json`，或 `%APPDATA%\Code\User\mcp.json` | `servers` | **必须带 `"type": "stdio"`** |

表里没有你的客户端时，停下来问用户，不要自己发明路径。

注意 Trae 是 VS Code 分支，但它的 MCP 文件用的是 `mcpServers`，不是 VS Code 本体的 `servers`。写错会静默不生效。

### 3. 合并条目

键名用 `calabiyau-map-engine`，内容是：

```json
{
  "command": "<MCP_REPO>/.venv/Scripts/python.exe",
  "args": ["-m", "calabiyau_map_engine_mcp", "--transport", "stdio"]
}
```

VS Code 的目标文件里要多加 `"type": "stdio"`。macOS / Linux 上 `command` 换成
`<MCP_REPO>/.venv/bin/python`。

硬性要求：

- `command` 必须是**绝对路径**。Windows 上正斜杠可以，反斜杠必须转义成 `\\`。
- **只增改这一个键**。文件里已有的其他 server（例如 `xerj`）必须原样保留。
- 写之前把原文件备份为 `<文件名>.bak-<时间戳>`。
- 文件不存在就新建，并建好父目录。
- 现有文件不是合法 JSON 时**停下来报告**，不要覆盖。
- **不要**把 `pairing_token` 或 `http_token` 写进配置。stdio 传输不需要凭据，凭据只给浏览器用。

### 4. 收尾

告诉用户三件事：

1. 必须**完全重启**客户端（托盘也要退），配置才会加载。
2. 重启后回来找你验证。
3. 现在还没完成——浏览器还没配对，工具能列出但调不动。

### 5. 重启后的验证

用户回来说"验证"时：

- 调 `list_sessions`。返回 `{"sessions": []}` 是**正常且成功**的：说明服务起来了，只是浏览器还没连。
- 如果工具列表里根本没有 `list_sessions` / `get_state`，说明配置没生效。让用户检查：`command` 路径是否存在、JSON 是否合法、客户端是否真的重启了。
- 报 `Startup failed (OSError)` 或服务起不来，通常是 **8765 端口被占用**——有另一个实例（可能是之前手动起的）还在跑，先关掉它。

### 6. 引导浏览器配对

1. 让用户在浏览器打开编辑器（开发环境是 `http://127.0.0.1:5173`，线上是 `https://klbqmap.miyo.wiki`），并打开一张地图。
2. 令牌在 `%LOCALAPPDATA%\CalabiYauMapEngineMCP\credentials.json` 的 `pairing_token` 字段。你能读就直接读；读不到就让用户执行
   `uv run python -m calabiyau_map_engine_mcp pair`，把他看到的 `pairing_token` 贴给你。
3. 让用户在编辑器里进 **设置 → MCP**，地址填 `ws://127.0.0.1:8765/bridge`，令牌填上面那个，点"连接并配对"。
4. 再调一次 `list_sessions`，这次应该返回一条带 `browser_session_id` 的记录。到这一步才算装完。

### 边界

- 这个服务只能连本机浏览器，不要尝试远程访问或改端口。
- 安装阶段不要调用任何写操作（`place_actor`、`delete_actors` 等）。
- 不要建议用户为了省事去复制别人的凭据文件——每台机器应该有自己的。

---

## 完整版提示词（客户端读不了本地文件时用）

```text
帮我把本机的 CalabiYau Map Engine MCP 服务注册进你当前运行的客户端配置。只做安装，不要动地图。

仓库路径：<MCP_REPO>。这个路径不存在就先问我，不要猜。
先确认 <MCP_REPO>/.venv/Scripts/python.exe 存在；不存在就在仓库根目录执行 uv sync --python 3.11。
不要用系统 python，也不要 uv run——客户端拉起服务时不会继承你的 shell 环境。

第一步，判断你运行在哪个客户端里。不确定就问我，不要猜。

第二步，找到该客户端的 MCP 配置文件：
  Trae / Trae CN     %APPDATA%\Trae CN\User\mcp.json        顶层键 mcpServers
  Cursor             %USERPROFILE%\.cursor\mcp.json         顶层键 mcpServers
  Claude Desktop     %APPDATA%\Claude\claude_desktop_config.json  顶层键 mcpServers
  Claude Code        项目根 .mcp.json                        顶层键 mcpServers
  VS Code / Copilot  项目内 .vscode\mcp.json                 顶层键 servers，条目必须带 "type": "stdio"
表里没有就停下来问我客户端名字，不要自己发明路径。
注意 Trae 是 VS Code 分支，但它用 mcpServers 而不是 servers。

第三步，往配置里合并这一条，键名用 calabiyau-map-engine：
  {
    "command": "<MCP_REPO>/.venv/Scripts/python.exe",
    "args": ["-m", "calabiyau_map_engine_mcp", "--transport", "stdio"]
  }
要求：
  - command 用绝对路径，反斜杠要转义成 \\；macOS/Linux 用 <MCP_REPO>/.venv/bin/python。
  - 只增改这一个键，已有的其他 server（比如 xerj）原样保留。
  - 写之前把原文件备份成 <文件名>.bak-<时间戳>。
  - 文件不存在就新建并建好父目录；现有文件不是合法 JSON 就停下来报告，不要覆盖。
  - 不要把 pairing_token 或 http_token 写进配置，stdio 不需要。

第四步，告诉我必须完全重启客户端（托盘也要退），因为 MCP 配置只在启动时读取，
你没法在本次会话里加载自己刚写的配置。

第五步，等我重启回来找你时再验证：
  - 调 list_sessions，返回 {"sessions": []} 是正常的，说明服务通了、浏览器还没连。
  - 工具列表里没有 list_sessions 说明配置没生效，检查路径、JSON、是否真的重启。
  - 起不来且报 Startup failed (OSError)，通常是 8765 端口被占用，先关掉别的实例。

第六步，带我完成浏览器配对：
  - 令牌在 %LOCALAPPDATA%\CalabiYauMapEngineMCP\credentials.json 的 pairing_token 字段。
  - 你读不到就让我执行 uv run python -m calabiyau_map_engine_mcp pair，把打印的 pairing_token 贴给你。
  - 让我在编辑器里进 设置 → MCP，地址填 ws://127.0.0.1:8765/bridge，令牌填上面那个，点连接并配对。
  - 最后再调一次 list_sessions，应该返回带 browser_session_id 的记录，才算装完。

不要尝试远程访问或改端口；不要建议我复制别人的凭据文件。
```
