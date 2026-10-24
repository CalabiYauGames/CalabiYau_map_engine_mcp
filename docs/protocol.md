# 浏览器桥接协议 v1

## 首帧认证

浏览器连接后，必须在服务端认证超时前发送：

```json
{
  "type": "hello",
  "version": 1,
  "token": "<pairing_token>",
  "session_id": "8f56d955-cde1-4cf4-8bb6-02a6259f93d4",
  "label": "CalabiYau Map Engine"
}
```

`session_id` 由浏览器标签页生成，是当前标签页的连接标识，长度为 `1..128`，仅允许字母、数字、`_` 和 `-`。页面内重连复用该值；重新加载或新标签页可生成新值。`label` 最长 `128` 字符，只供展示，不参与鉴权。

配对成功时，服务返回：

```json
{
  "type": "welcome",
  "version": 1,
  "session_id": "8f56d955-cde1-4cf4-8bb6-02a6259f93d4"
}
```

服务将该连接注册为 `browser_session_id`，MCP 客户端通过 `list_sessions` 发现它。同一浏览器会话标识的新连接替换旧连接；旧连接的未完成请求失败，不转移到新连接。

认证超时、错误 token 或非法消息以策略错误关闭；连接没有完成 `welcome` 前，浏览器不执行任何地图请求。

### 记住配对

用户在网页设置中明确连接后，网页按自身来源保存连接偏好和 token。后续在浏览器允许本地网络访问时，可以自动恢复连接并执行编辑动作。

页面“忘记配对”只清除本浏览器该来源下的偏好。服务端 `reset-pairing` 轮换两个 token，适用于撤销所有持有旧凭据的页面和 HTTP 客户端；应先停止旧服务，再轮换并重启。

## 请求、响应与心跳

Python 向浏览器发送：

```json
{
  "type": "request",
  "version": 1,
  "id": "c01d6878-56ea-4224-aa42-9ac2fd77652b",
  "action": "get_state",
  "params": {}
}
```

`id` 是 Python 为本次桥接调用生成的独立 UUID，不是 MCP 客户端的 JSON-RPC ID。请求还在服务端绑定到确切的浏览器连接，其他连接不能完成它。

成功响应：

```json
{
  "type": "response",
  "version": 1,
  "id": "c01d6878-56ea-4224-aa42-9ac2fd77652b",
  "ok": true,
  "result": {
    "map_session_id": "ca8cbb99-5bc6-4d19-bf6b-92c919b67161",
    "revision": 7,
    "map": {
      "id": "current-map-id",
      "name": "示例地图"
    }
  }
}
```

示例 `result` 只列出部分状态字段。失败响应：

```json
{
  "type": "response",
  "version": 1,
  "id": "c01d6878-56ea-4224-aa42-9ac2fd77652b",
  "ok": false,
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "The map changed; query its current state before retrying.",
    "details": {
      "map_session_id": "ca8cbb99-5bc6-4d19-bf6b-92c919b67161",
      "revision": 8
    }
  }
}
```

`error.code` 稳定用于程序分支，`message` 只供展示，不能依赖其语言或精确文本。`details` 可省略。Python 转换到 MCP 错误时可能把浏览器细节放在 `browser_details` 中。

服务约每 `15` 秒发送 `{"type":"ping","version":1}`；浏览器回复 `{"type":"pong","version":1}`。长时间未收到有效消息的连接会关闭，浏览器可按退避策略重新连接。认证或来源被拒绝时，不应无限重试。

### 超时与重试

- 服务端请求默认 `30` 秒超时，可通过 CLI 修改；浏览器自身也有操作超时保护。
- 同一浏览器的写请求在 Python 内串行执行，覆盖全部 MCP 传输入口。
- 浏览器对同一个请求 ID 缓存响应，避免在当前连接中重复执行；这不是跨重启的事务保证。
- 未知、重复或已经过期的响应不会完成其他请求。
- 断线、超时或取消不代表地图没有修改。结果无法确认时，必须先重新查询地图状态，禁止自动重放写请求。
- 浏览器重连不恢复或补发旧的写请求。MCP 调用方应重新取得会话和修订信息。

## 地图动作

MCP 层除 `list_sessions` 外都要求 `browser_session_id`。Python 负责会话路由，转发给浏览器的 `params` 不再包含该路由字段。

### 只读动作

| 动作 | 浏览器参数 | 结果 |
| --- | --- | --- |
| `get_state` | 空对象 | 当前地图、`map_session_id`、`revision`、选区及总数、脏状态、编辑/忙碌/只读/预览状态、撤销能力和格子范围 |
| `list_items` | 可选 `query`、`offset`、`limit` | `items`、`offset`、`limit`、`total` 和地图快照标记 |
| `list_actors` | 可选 `item_id`、`selected_only`、`offset`、`limit`、`map_session_id`、`expected_revision` | `actors`、分页信息和地图快照标记 |

`get_state` 允许没有地图时返回 `map: null`、`map_session_id: null`。`list_actors` 要求先打开地图；`list_items` 要求物品目录已经加载。

选区结果中的 `selection` 最多返回 `256` 个编辑器标识，`selection_count` 是实际总数，`selection_truncated` 表示是否截断。需要完整列表时，使用 `list_actors` 的 `selected_only: true` 分页查询。

分页 `offset` 默认 `0`，`limit` 默认 `100`，范围 `1..500`。物品查询字符串最长 `200` 字符。需要一致的多页 Actor 查询时，传入首个快照的 `map_session_id` 和 `expected_revision`；地图变化后会拒绝后续旧快照查询，而不是静默混合两个版本。

Actor 摘要：

| 字段 | 语义 |
| --- | --- |
| `editor_id` | 当前编辑器模型中的运行时标识，供 `actor_ids` 使用，不是原文件 `actorId` |
| `item_id` | 物品目录标识 |
| `cell` | 地图格子坐标 `[x, y, z]` |
| `transform_mode` | 现有原生持久变换模式 |
| `transform` | 格子/旋转或原生厘米位置等对应模式数据 |
| `color_linear_rgb` | 原生线性 float32 RGB 或 `null`，不是显示用的 `0..255` 字节颜色 |
| `selected` | 当前是否选中 |
| `editable` | 是否可通过 MCP 修改，保留或投影 Actor 为 false |

所有整数都必须是 JavaScript 安全整数。地图格子与 Three.js 场景坐标不是相同坐标体系，不得把摄像机或渲染坐标直接作为 `cell`。

### 写动作的共同前提

每个写动作都要求：

```json
{
  "map_session_id": "<get_state 返回的地图会话>",
  "expected_revision": 7
}
```

地图会话识别编辑模型，不是文件名；切换到别的地图后不能继续沿用旧会话。`expected_revision` 是最近观察到的非负修订号，覆盖实际地图和相关项目状态。选择变化也需要重新使用操作返回的修订号。

编辑器必须有当前地图、处于编辑模式、未忙碌且可写。生成结果预览、预制件阶段、只读投影或正在进行的编辑任务不能通过 MCP 强行修改。

| 动作 | 附加参数 | 结果 |
| --- | --- | --- |
| `select_actors` | `actor_ids`，可选 `mode: replace/add/toggle`，默认 `replace` | `selection`、`selection_count`、`selection_truncated` |
| `place_actor` | `item_id`、`cell`，可选 `rotation_index`、`color_linear_rgb` | 新建 `actor`，未创建时为 `null` |
| `move_actors` | `actor_ids`、整数格子增量 `delta` | 移动后的 `actors` |
| `delete_actors` | `actor_ids` | 实际删除的 `deleted_actor_ids` |
| `undo` | 无 | `can_undo`、`can_redo` |
| `redo` | 无 | `can_undo`、`can_redo` |

写结果还包含当前 `map_session_id`、`revision`、`changed`。空撤销栈或无变化的合法操作可返回 `changed: false`，不能据此假定一定报错。

`actor_ids` 不允许重复。选择允许 `0..256` 个标识；`replace` 加空数组清除选区。移动和删除要求 `1..256` 个标识。不存在、保留或投影 Actor 会在执行前被拒绝，不允许只成功其中一部分。

放置只支持目录中可独立放置的单个物件；成对套件和保留设置项不在首版范围。目标格子必须有效且空闲，不覆盖已有物件。`rotation_index` 范围 `0..3`，默认 `0`。`color_linear_rgb` 默认白色，必须是三个有限 float32 数值；对于不可染色物件使用其现有默认行为。

移动沿地图格子执行，并保留现有碰撞和边界检查。删除会沿用编辑器的成对关系处理，可能同时删除关联物件，因此以实际返回的标识列表为准。

撤销和重做走现有编辑器历史，可能撤销用户之前手动完成的操作，不是 MCP 私有历史。所有动作都使用现有状态发布和渲染更新流程。

## 错误处理

| 错误码 | 调用方处理 |
| --- | --- |
| `BROWSER_NOT_CONNECTED`、`BROWSER_DISCONNECTED` | 重新查询连接会话；已发出的写请求不要自动重放 |
| `BROWSER_TIMEOUT`、`TIMEOUT` | 结果可能未知，先查询状态 |
| `NO_MAP` | 请用户在网页打开地图 |
| `STALE_SESSION`、`REVISION_CONFLICT` | 重新读取当前地图状态并重新评估操作 |
| `EDIT_MODE_REQUIRED` | 请用户在网页进入编辑模式 |
| `READ_ONLY`、`UNSUPPORTED_PREVIEW` | 尊重编辑器限制，不通过其他入口绕过 |
| `EDITOR_BUSY`、`BUSY` | 等待当前任务完成后重新读取状态 |
| `CATALOG_UNAVAILABLE` | 等待或补齐编辑器物品目录 |
| `INVALID_PARAMS`、`ACTOR_NOT_FOUND`、`ACTOR_NOT_EDITABLE` | 修正输入或重新查询 Actor |
| `ITEM_NOT_PLACEABLE`、`CELL_OCCUPIED`、`OUT_OF_BOUNDS`、`MOVE_REJECTED` | 根据目录和当前格子状态重新选择操作 |
| `HISTORY_FAILED` | 编辑器组合历史未能完成；读取当前状态 |
| `UNKNOWN_ACTION`、`UNSUPPORTED_ACTION` | 不调用白名单之外的动作 |
| `RESPONSE_LIMIT`、`INVALID_MESSAGE`、`INTERNAL_ERROR` | 减少请求范围或排查实现 |

协议没有 `save`、`export`、`eval`、`execute_javascript` 或通用函数名转发入口。未来增加动作需要同时更新服务端工具注册、浏览器动作白名单、领域 adapter。
