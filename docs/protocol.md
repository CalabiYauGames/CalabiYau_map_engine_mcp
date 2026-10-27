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
| `list_links` | 可选 `actor_ids`、`kind`、`offset`、`limit`、`map_session_id`、`expected_revision` | `links`、分页信息和地图快照标记 |
| `capture_view` | 可选 `position`+`target`、`width`、`height`、`format`、`quality`，以及服务端下发的 `upload_url`、`capture_id` | `camera`，加上 `uploaded`（新）或 `image.data_url`（旧） |

#### 截图的 HTTP 通道

桥接帧上限是 1 MiB，而一张 1400×1000 的 PNG 密集模型有两兆多——这正是过去所有截图都被迫降级成小 JPEG 的原因，而小 JPEG 恰恰看不清 0.15 米一块的板。

所以图像字节不走 WebSocket。`capture_view` 在派发之前先向 `CaptureRelay` 申请一个一次性槽位，把绝对地址 `upload_url` 和 `capture_id` 一起放进 `params`；浏览器把图像 POST 到 `POST /capture/{capture_id}`，然后只回一个 `uploaded` 元数据块（几十字节）；工具再从槽位取字节落盘。

- 槽位 id 是 24 字节的 urlsafe 令牌，**只能用一次**，取走后立刻释放；超时（默认 120 秒）也会释放，迟到的 POST 得到 404 而不是写进一个没人读的槽位。
- 单张上限 64 MiB（`capture.py` 的 `MAX_CAPTURE_BYTES`），超了返回 413。
- 浏览器拿到的 host 来自 WebSocket 握手时的 `Host` 头，所以 `localhost` 和 `127.0.0.1` 两种写法都能回连。
- 老标签页不认识 `upload_url` 时仍然回 `image.data_url`，工具照旧解码——混合版本不会炸，只是又回到帧上限。

`/capture/*` 不走 MCP 的 Bearer 校验，靠的是一次性槽位本身，加上 `LocalSecurity` 对 `Host`/`Origin` 的检查。

`get_state` 允许没有地图时返回 `map: null`、`map_session_id: null`。`list_actors`、`list_links` 要求先打开地图；`list_items` 要求物品目录已经加载。

选区结果中的 `selection` 最多返回 `256` 个编辑器标识，`selection_count` 是实际总数，`selection_truncated` 表示是否截断。需要完整列表时，使用 `list_actors` 的 `selected_only: true` 分页查询。

分页 `offset` 默认 `0`，`limit` 默认 `100`，范围 `1..500`。物品查询字符串最长 `200` 字符。需要一致的多页 Actor 查询时，传入首个快照的 `map_session_id` 和 `expected_revision`；地图变化后会拒绝后续旧快照查询，而不是静默混合两个版本。

Actor 摘要：

| 字段 | 语义 |
| --- | --- |
| `editor_id` | 当前编辑器模型中的运行时标识，供 `actor_ids` 使用，不是原文件 `actorId` |
| `item_id` | 物品目录标识 |
| `cell` | 地图格子坐标 `[x, y, z]` |
| `transform_mode` | 现有原生持久变换模式，`1` 格子对齐、`2` 完整浮点、`3` 整数缩放 |
| `transform` | 与模式对应的姿态数据，形状见下 |
| `color_linear_rgb` | 原生线性 float32 RGB 或 `null`，不是显示用的 `0..255` 字节颜色 |
| `selected` | 当前是否选中 |
| `editable` | 是否可通过 MCP 修改，保留或投影 Actor 为 false |

物品摘要中的 `connect_item_id` 是原生 `ConnectItemID`（功能套件伙伴物品，`0` 表示无），`child_item_id` 是原生 `ChildItemID`（本物品作为连接主件时接受的子件物品，`0` 表示不能作主件）。这两个字段只描述物品契约，不表示当前地图里已经存在连接。

`build_kind` 是适配层加的搭建分类，取值 `voxel`、`component`、`functional`。物品表本身没有占地数据（`blueprint` 是资源路径而不是包围盒），所以这个字段来自 `CalabiYau_Map_Engine/src/mcp/itemKind.ts` 里的显式清单，并且**默认保守**：不在 `voxel` 清单里的一律是 `component`。`voxel` 是真正的 1×1 格子方块，可以任意平铺；`component` 有自身占地、朝向或缩放；`functional` 是玩法件。批量几何工具只应该接收 `voxel`。

### 姿态形状

`transform` 随 `transform_mode` 变化：

| 模式 | 形状 | 说明 |
| --- | --- | --- |
| `1` | `{ cell, rotation_index }` | 格子对齐，只有四分之一圈朝向 |
| `2` | `{ location_cm, scale, rotation_degrees }` | 完整浮点：厘米位置、三轴缩放、三轴角度 |
| `3` | `{ location_cm, scale }` | 整数缩放轴对齐，MCP 不提供写入 |

`location_cm` 是 Actor 自身 pivot 在 UE 厘米空间的位置，`rotation_degrees` 是 UE 的 pitch/yaw/roll（度）。模式 `2` 的 Actor **不占格**：`cell` 只是由位置反推出来的近似值，用于分页和选区显示，不参与占用判断，所以多块面板可以叠在同一个 `cell` 上。

物品摘要里的 `free_transform` 表示该物品的 `transform_mode` 是否为 `2`，即能否用 `transform` 放置；`local_extents_cm` 只在适配层实测过几何的物品上出现，给出本地包围盒（`x`/`y`/`z` 各是 `[min, max]` 厘米），调用方可以用它算落地高度和判界。


所有整数都必须是 JavaScript 安全整数。地图格子与 Three.js 场景坐标不是相同坐标体系，不得把摄像机或渲染坐标直接作为 `cell`。

### 连接摘要

`list_links` 返回编辑器原生的链接，`links` 每项形如：

```json
{ "kind": "group", "main_actor_id": 3, "child_actor_ids": [4, 5] }
```

| `kind` | 含义 | 写入地图 |
| --- | --- | --- |
| `pair` | `ConnectItemID` 功能套件的两半，伙伴物品没有建造菜单条目 | 是 |
| `group` | `ChildItemID` 编组，`child_actor_ids` 的顺序就是路径顺序 | 是 |
| `orphan` | 编组在某处断开后残留的编辑器修复状态，`main_actor_id` 为 `null`，`child_actor_ids` 是残留节点 | 否 |

链接顺序就是编辑器的插入顺序，不做额外排序。一个 Actor 最多出现在一条链接里。预览和预制件阶段的投影链接不属于持久地图，不会出现在结果中。`actor_ids` 只按成员过滤，不校验这些标识是否存在；`kind` 只接受 `pair`、`group`、`orphan`。需要一致的多页查询时，同样用 `map_session_id` 和 `expected_revision` 钉定快照。

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
| `place_actor` | `item_id` 与 `cell` 或 `transform` 二选一，可选 `rotation_index`、`color_linear_rgb` | 新建 `actor`，未创建时为 `null` |
| `place_actors` | `placements`，最多 4096 项；每项含 `item_id` 与 `cell` 或 `transform`，可选 `rotation_index`、`color_linear_rgb` | 原子批量返回 `placed_actors`、`placement_errors`；一次请求、一次修订、一次撤销记录。逐项失败包含 `index` 和 `reason`，不将整个 MCP 工具标为错误 |
| `place_quad` | `vertices`：四个 `[x, y, z]` 厘米角点，可选 `color_linear_rgb` | 新建 `actor`；由角点算出 `location_cm`/`scale`/`rotation_degrees` 后走 `place_actors` |
| `place_quads` | `quads`：`{vertices, color_linear_rgb?}` 数组，最多 4096 项 | `placed_actors`、`placement_errors`，另加 `validation_errors`、`placed_count`、`failed_count` |
| `update_actors` | `actor_ids`，以及 `color_linear_rgb`、`rotation_index`、`item_id`、`params` 之一，或自由姿态的 `location_cm`/`location_delta_cm`/`scale`/`rotation_degrees` | 更新后的 `actors` |
| `move_actors` | `actor_ids`、整数格子增量 `delta` | 移动后的 `actors`（**可能已被钳制**，以返回值为准） |
| `delete_actors` | `actor_ids` | 实际删除的 `deleted_actor_ids` |
| `create_scene_object` | `name`、`actor_ids` | 新建工程对象 `object_id` 与 `actor_ids` |
| `delete_scene_object` | `object_id` | 被删除的 `object_id`，同时移除其成员 Actor |
| `connect_actors` | `main_actor_id`、`child_actor_ids` | `links`（操作后相关链接） |
| `disconnect_actors` | `actor_ids` | `links`（操作后相关链接） |
| `undo` | 无 | `can_undo`、`can_redo` |
| `redo` | 无 | `can_undo`、`can_redo` |

写结果还包含当前 `map_session_id`、`revision`、`changed`。空撤销栈或无变化的合法操作可返回 `changed: false`，不能据此假定一定报错。

`actor_ids` 不允许重复。选择允许 `0..256` 个标识；`replace` 加空数组清除选区。移动和删除要求 `1..256` 个标识。不存在、保留或投影 Actor 会在执行前被拒绝，不允许只成功其中一部分。

放置只支持目录中可独立放置的单个物件；成对套件和保留设置项不在首版范围。目标格子必须有效且空闲，不覆盖已有物件。`rotation_index` 范围 `0..3`，默认 `0`。`color_linear_rgb` 默认白色，必须是三个有限 float32 数值；对于不可染色物件使用其现有默认行为。

自由姿态（`transform`）只对 `transform_mode: 2` 的物品开放，其它物品报 `ITEM_NOT_FREELY_PLACEABLE`。`scale` 每个轴允许 `0`（零厚度面板）到 `10000`，`location_cm` 每个轴限制在 `±1000000`。有实测包围盒的物品会按缩放后的实际盒判断是否出界，越界报 `OUT_OF_BOUNDS` 并带上 `axes`、`actor_bounds` 与 `map_bounds`；没有实测盒的物品不做范围猜测。`cell` 与 `transform` 同时给出，或同时缺失，都是 `INVALID_PARAMS`。

`update_actors` 的自由字段与 `rotation_index` 互斥，`location_cm` 与 `location_delta_cm` 互斥。只要本次请求带了任一自由字段，全部目标都必须是模式 `2`，否则整体拒绝并返回 `ACTOR_NOT_FREELY_PLACEABLE` 与 `editor_ids`、`transform_modes`——不做「能改的改，不能改的跳过」。对模式 `2` 传 `rotation_index` 会写成对应的 yaw，不会静默丢弃。

移动沿地图格子执行。整数增量会按**整个选区的边界**钳制，而不是让每个物件各自钳到地图边——后者会把一个密集选区折成一条线。因此实际位置一律以返回的 `actors` 为准；目标格被占用、增量不是整数（针对格子对齐物件）或编辑器拒绝时报错。删除会沿用编辑器的成对关系处理，可能同时删除关联物件，因此以实际返回的标识列表为准。

`place_quad` 与 `place_quads` 只产出 **item 7**（斜坡/面板）：`quad_to_transform` 用 item 7 实测的 1 m × 1 m 面计算缩放，换别的物品不成立。四个角点必须构成平面矩形（对边等长且相邻边垂直），否则报 `INVALID_GEOMETRY`。它们是 `place_actor`/`place_actors` 的顶点输入版本，最终仍走同一条原子批量通路。

`create_scene_object` 与 `delete_scene_object` 操作的是**工程对象**（durable project group），不是原生连接；删除会连同成员 Actor 一起移除，并可通过 `undo` 撤销。

`connect_actors` 直接调用编辑器的连接操作：主件的物品必须声明 `child_item_id`，每个子件的物品必须等于该值，`child_actor_ids` 的顺序就是编组路径顺序，`1..256` 个且不得重复、不得包含主件。它是**替换式**的——原来属于别的编组的成员会迁移过来，被切掉的旧成员如果还有连续段，会变成 `orphan` 出现在本次返回的 `links` 里。原生连接会给还没有 `actorId` 的成员分配一个，这是编辑器保存编组所必需的。

请求的编组已经成立时，浏览器不再执行一次空变更：不新增历史记录、不递增修订号、不清空重做栈，直接返回 `changed: false`。因此按同一组参数重复调用是安全的。

成对套件（`pair`）不能通过 `connect_actors` 建立：伙伴物品没有建造菜单条目，只能由编辑器成对放置。`connect_actors` 也会拒绝任何已经处于 `pair` 中的成员，因为原生重建会静默丢弃那条 pair 链接，留下无法单独存在的伙伴物件；需要拆开时先调用 `disconnect_actors`。

`disconnect_actors` 调用编辑器的断开操作：`pair` 整条丢弃但两个 Actor 都保留，`group` 在断开处切段，断点之后的连续段变成 `orphan`。返回的 `links` 覆盖操作前涉及这些标识的全部成员，所以切出来的残留段也在其中。对本来没有任何链接的 Actor 调用是合法的无变化操作，返回 `changed: false`。

撤销和重做走现有编辑器历史，可能撤销用户之前手动完成的操作，不是 MCP 私有历史。所有动作都使用现有状态发布和渲染更新流程。

### 模型导入

模型文件走四步，因为桥接单帧上限是 1 MiB，而建筑模型不是：

| 动作 | 参数 | 结果 |
| --- | --- | --- |
| `upload_model` | `data_base64`、`offset`；首片另带 `name` 与 `byte_length`，后续带 `upload_id` | `upload_id`、`received`、`total`、`complete`、`chunk_bytes` |
| `plan_model` | `upload_id`、`options` | `upload_id` 与 `plan`：块数、跨度、格子包围盒、统计与告警 |
| `apply_model` | `upload_id`、`overwrite`，以及标准快照字段 | `placed_count`、`planned_blocks`、`planned_revision`、`plan` |
| `discard_model` | `upload_id` | `released` 与仍持有的 `held` |

`upload_model` 用调用方给的 `upload_id` 作为键：分片必须严格接在已收字节之后，乱序或部分重叠会报 `INVALID_PARAMS`；**完全落在已收范围内的重发是幂等的**，直接返回当前进度，所以丢响应后重试不会把字节写两遍。文件名必须带受支持的扩展名（`glb`、`gltf`、`obj`、`stl`、`fbx`、`ply`、`vox`），总大小上限 64 MiB。

`plan_model` 是唯一的异步动作：浏览器把文件解析后交给 Worker 体素化，可能超过 30 秒的请求超时。它不写地图，因此不要求快照字段；方案按 `upload_id` 留在浏览器里，超时后可以用同一个 id 再问一次，不必重新上传。`options` 里 `origin` 必填，`mode` 为 `adaptive`、`voxel` 或 `full-size`；普通方块都是一格，所以 `resolution` 同时就是成品高度（格），只有 `full-size` 接受 `target_size`。

`apply_model` 是唯一写地图的一步：它要求 `map_session_id` 与 `expected_revision`，校验方案是在同一地图会话里解出来的，并且在 `overwrite` 不是 `true` 时拒绝覆盖已被占用的格子（`CELL_OCCUPIED`，带 `occupied_blocks` 与 `sample_cells`）。整批放置是一次修订、一条撤销记录，成功后释放该上传。

对外的 `import_model` 把四步收成一个工具：给 `path` 就是上传并解算，给 `plan_token` 就是应用已经解出来的方案。**只有给 `path` 时才需要 `origin`**——方案里已经带着拟合时的原点，再让调用方重复一遍既没用也容易写错。`discard_model` 单独暴露，用来丢掉一个不打算用的上传（浏览器最多同时持有三份）。

模型的面绕序必须朝外。绕序反了会让拟合看到内表面，成品是错的而**不会报错**：`full-size` 会退化成按体素合并的阶梯，`adaptive` 会几乎没有斜板。手工写 `.obj` 时最容易犯这个错，成品看着像一栋歪楼而不是一块块板。

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
| `CATALOG_UNAVAILABLE` | 等待或补齐编辑器物品目录；若地图引用了目录里不存在的物品，重试不会恢复，需要目录与地图匹配 |
| `INVALID_PARAMS`、`ACTOR_NOT_FOUND`、`ACTOR_NOT_EDITABLE` | 修正输入或重新查询 Actor |
| `ITEM_NOT_PLACEABLE`、`CELL_OCCUPIED`、`DUPLICATE_CELL`、`OUT_OF_BOUNDS`、`MOVE_REJECTED` | 根据目录和当前格子状态重新选择操作 |
| `ITEM_NOT_FREELY_PLACEABLE`、`ACTOR_NOT_FREELY_PLACEABLE` | 该物品/物件持久的是格子或整数缩放模式，存不下缩放和任意角；改用 `cell` 放置 |
| `ACTOR_NOT_DUPLICABLE` | 编辑器没有报告这个物件的 pivot、缩放和角度，无法重建；改用逐块放置 |
| `MODEL_UPLOAD_UNKNOWN`、`MODEL_UPLOAD_INCOMPLETE`、`MODEL_UPLOAD_TOO_LARGE` | 上传会话已过期、字节还没收齐或超过 64 MiB；重新上传 |
| `MODEL_PLAN_UNKNOWN` | 该 `upload_id` 还没有方案；先调用 `plan_model`，或在超时后重试同一个 id |
| `MODEL_IMPORT_FAILED`、`MODEL_ITEM_UNSUPPORTED`、`MODEL_OUT_OF_BOUNDS` | 模型解析或解算失败、方块不在实测清单里、成品尺寸超出地图；按 `details` 调整 `resolution`、`item_ids` 或 `origin` |
| `BATCH_TOO_LARGE` | 将 `place_actors` 拆成不超过 4096 项的批次；每个批次独立修订和撤销记录 |
| `ITEM_NOT_CONNECTABLE`、`CHILD_ITEM_MISMATCH` | 主件不声明 `child_item_id`，或子件物品不是主件声明的子件；用 `list_items` 重新挑选 |
| `ACTOR_IN_PAIR` | 成员是成对套件的一半；先 `disconnect_actors` 拆开，或改用另一组 Actor |
| `CONNECT_REJECTED`、`DISCONNECT_REJECTED` | 防御性错误码，正常实现下不会出现；出现说明编辑器拒绝了已通过校验的变更，读取当前状态后重新评估 |
| `HISTORY_FAILED` | 编辑器组合历史未能完成；读取当前状态 |
| `UNKNOWN_ACTION`、`UNSUPPORTED_ACTION` | 不调用白名单之外的动作 |
| `RESPONSE_LIMIT`、`INVALID_MESSAGE`、`INTERNAL_ERROR` | 减少请求范围或排查实现 |

协议没有 `save`、`export`、`eval`、`execute_javascript` 或通用函数名转发入口。未来增加动作需要同时更新服务端工具注册、浏览器动作白名单、领域 adapter。
