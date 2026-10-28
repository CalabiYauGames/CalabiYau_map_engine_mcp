# MCP Tools Complete Reference

这是CalabiYau Map Engine MCP的完整工具参考。当你需要详细了解某个MCP工具的用法时，来这里查阅。

## 快速导航

- **会话管理**：[list_sessions](#list_sessions)、[get_state](#get_state)
- **核心建模**：[place_actors](#place_actors)、[update_actors](#update_actors)、[delete_actors](#delete_actors)
- **查询工具**：[list_items](#list_items)、[list_actors](#list_actors)
- **视图工具**：[set_camera](#set_camera)
- **高级操作**：[copy_actors](#copy_actors)、[move_actors](#move_actors)、[array_actors](#array_actors)、[mirror_actors](#mirror_actors)

---

## 会话管理工具

### list_sessions

**用途**：获取当前连接的浏览器会话列表。**必须首先调用**。

**参数**：无

**返回**：
```json
{
  "sessions": [
    {
      "browser_session_id": "string",
      "paired_tab": true,
      "map_session_id": "string"
    }
  ]
}
```

**重要**：
- 如果返回空列表，说明没有浏览器连接 → 用户需要打开Map Engine并配对
- 不要猜测session_id或循环重试
- 总是取第一个session

**示例**：
```javascript
const sessions = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_sessions",
  args: {}
});

if (!sessions.sessions.length) {
  throw new Error("Please open Map Engine in browser first");
}

const browser_session_id = sessions.sessions[0].browser_session_id;
```

---

### get_state

**用途**：读取地图的实时状态。提供每次写入操作所需的`map_session_id`和`expected_revision`。

**参数**：
```json
{
  "browser_session_id": "string"
}
```

**返回**：
```json
{
  "map_session_id": "string",
  "revision": 12345,
  "grid": {...},
  "selection": {...},
  "editing": true,
  "read_only": false,
  "busy": false,
  "busy_reasons": [],
  "persistent_actor_count": 1024,
  "can_undo": true,
  "can_redo": false,
  ...
}
```

**关键字段**：
- `map_session_id`：识别当前地图
- `revision`：**当前修订版本号**（用于乐观锁）
- `editing`：是否处于编辑模式
- `persistent_actor_count`：已放置的actor总数

**何时调用**：
1. 在`list_sessions`之后立即调用（初始化）
2. 每次`place_actors`或`update_actors`之前（获取最新revision）
3. 发生`REVISION_CONFLICT`错误后

**示例**：
```javascript
const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});

const { map_session_id, revision: expected_revision } = state;
```

---

## 核心建模工具

### place_actors

**用途**：批量放置actor（最常用的建模工具）。一次调用最多4096个placement。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "placements": [
    {
      "item_id": 7,
      "transform": {
        "location_cm": [6400, 6400, 6400],
        "scale": [1.0, 200, 200],
        "rotation_degrees": [0, 0, 0]
      },
      "color_linear_rgb": [5.319, 5.319, 5.714],
      "rotation_index": 0
    }
  ]
}
```

**placement对象字段**：

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `item_id` | integer | ✓ | 要放置的物品ID（建模用7） |
| `transform` | object | * | 对free_transform物品需要：`location_cm`、`scale`、`rotation_degrees` |
| `cell` | [x,y,z] | * | 对网格物品需要：目标网格坐标（互斥with transform） |
| `color_linear_rgb` | [r,g,b] | ✗ | 线性RGB颜色（见颜色转换指南） |
| `rotation_index` | 0-3 | ✗ | 四分之一转向（仅对网格物品有效） |

**transform字段详解**：

| 字段 | 单位 | 类型 | 范围 | 说明 |
|------|------|------|------|------|
| `location_cm` | 厘米 | [float,float,float] | [0,12800] | 世界坐标 |
| `scale` | 米 | [float,float,float] | ≥0 | [thickness, width, height] |
| `rotation_degrees` | 度 | [float,float,float] | [-360,360] | [pitch, yaw, roll] (虚幻引擎格式) |

**返回**：
```json
{
  "placed_actors": [
    {
      "editor_id": 123,
      "transform": {...},
      "item_id": 7
    }
  ],
  "placement_errors": [
    {
      "index": 0,
      "error": "OUT_OF_BOUNDS"
    }
  ],
  "new_revision": 12346
}
```

**错误处理**：

| 错误 | 原因 | 解决 |
|------|------|------|
| `OUT_OF_BOUNDS` | 坐标超出[0,12800] | 检查location_cm和scale |
| `REVISION_CONFLICT` | 地图被修改 | 重新get_state获取新revision |
| `ACTOR_PLACEMENT_FAILED` | 单个placement失败 | 检查placement_errors数组 |

**一键式建模工作流**：
```javascript
// 1. 获取session和revision
const sessions = await tools.run_mcp({...}); // list_sessions
const browser_session_id = sessions.sessions[0].browser_session_id;

const state = await tools.run_mcp({...}); // get_state
const { map_session_id, revision } = state;

// 2. 构建placement数组
const placements = [
  { item_id: 7, transform: {...}, color_linear_rgb: [...] },
  { item_id: 7, transform: {...}, color_linear_rgb: [...] }
];

// 3. 放置
const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision: revision,
    placements
  }
});
```

---

### update_actors

**用途**：修改已存在的actor（颜色、位置、大小、旋转）。最多256个actor/次。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "actor_ids": [123, 124, 125],
  "color_linear_rgb": [5.0, 2.5, 1.0],
  "location_cm": [6500, 6400, 6400],
  "location_delta_cm": [100, 0, 0],
  "scale": [1.0, 250, 250],
  "rotation_degrees": [45, 90, 0],
  "item_id": 7
}
```

**字段说明**：

| 字段 | 作用 | 说明 |
|------|------|------|
| `actor_ids` | 选择目标 | 最多256个（必须） |
| `color_linear_rgb` | 改颜色 | 仅对可着色物品有效 |
| `location_cm` | 绝对位置 | 所有actor堆叠到同一位置（谨慎！） |
| `location_delta_cm` | 相对移动 | **推荐**：每个actor移动相同偏移 |
| `scale` | 改大小 | [thickness, width, height] |
| `rotation_degrees` | 改旋转 | [pitch, yaw, roll] |
| `item_id` | 改类型 | 保留transform但改物品（谨慎！） |

**特殊参数**：

- `"expected_revision": "latest"` 跳过乐观锁检查（如果知道没有冲突）
- `rotation_index` (0-3)：快速四分之一转向

**常用场景**：

```javascript
// 场景1：改颜色
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "update_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision: revision,
    actor_ids: [123, 124, 125],
    color_linear_rgb: [5.0, 2.5, 1.0]
  }
});

// 场景2：移动（使用delta）
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "update_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision: revision,
    actor_ids: [123, 124, 125],
    location_delta_cm: [100, 50, 0]  // 每个移动(100, 50, 0)
  }
});

// 场景3：改大小和旋转
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "update_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision: revision,
    actor_ids: [123, 124, 125],
    scale: [2.0, 400, 400],
    rotation_degrees: [0, 45, 0]
  }
});
```

---

### delete_actors

**用途**：删除actor。最多256个/次。可撤销。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "actor_ids": [123, 124, 125]
}
```

**返回**：
```json
{
  "deleted_actor_ids": [123, 124, 125],
  "new_revision": 12346
}
```

**重要**：配对actor（如pair kit）会自动删除，所以实际删除的actor数可能多于请求的。

---

## 查询工具

### list_items

**用途**：查询物品目录。获取物品的能力、几何、颜色属性。

**参数**：
```json
{
  "browser_session_id": "string",
  "query": "slope",
  "offset": 0,
  "limit": 100
}
```

**查询示例**：
- `"7"` → 按ID查询
- `"slope"` → 按名字查询
- `"free"` → 按特性查询（free_transform）
- `""` → 列出所有

**返回**：
```json
{
  "items": [
    {
      "item_id": 7,
      "name": "Slope",
      "description": "斜坡/铁板",
      "build_kind": "component",
      "free_transform": true,
      "colorable": true,
      "placeable": true,
      "connect_item_id": 0,
      "child_item_id": 0,
      "local_extents_cm": {
        "min": [-25, -50, -50],
        "max": [0, 50, 50]
      }
    }
  ],
  "total": 512
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `item_id` | 物品ID（用于place_actors中的item_id） |
| `free_transform` | true=支持transform，false=仅支持cell |
| `colorable` | true=支持color_linear_rgb |
| `build_kind` | `"voxel"`=网格块，`"component"`=装饰，`"functional"`=功能 |
| `local_extents_cm` | scale=1时的几何边界（用于边界检查） |
| `connect_item_id` | ≠0表示配对工具包，MCP不能直接放置 |

**示例**：验证Item 7能力
```javascript
const items = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_items",
  args: {
    browser_session_id,
    query: "7",
    limit: 1
  }
});

const slope = items.items[0];
console.log(`Item 7: ${slope.name}`);
console.log(`Free transform: ${slope.free_transform}`); // true
console.log(`Colorable: ${slope.colorable}`); // true
console.log(`Extents: ${JSON.stringify(slope.local_extents_cm)}`);
```

---

### list_actors

**用途**：查询已放置的actor。支持分页、过滤、查询actor ID。

**参数**：
```json
{
  "browser_session_id": "string",
  "offset": 0,
  "limit": 100,
  "item_id": 7,
  "selected_only": false,
  "cell_box": {
    "min": [0, 0, 0],
    "max": [128, 128, 128]
  },
  "map_session_id": "string",
  "expected_revision": 12345
}
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `offset` | int | 分页起点（默认0） |
| `limit` | int | 每页数量，最多4096 |
| `item_id` | int | 仅查询特定物品（如7=仅查询斜坡） |
| `selected_only` | bool | 仅查询选中的 |
| `cell_box` | object | 仅查询特定区域 |

**返回**：
```json
{
  "actors": [
    {
      "editor_id": 123,
      "item_id": 7,
      "transform": {
        "location_cm": [6400, 6400, 6400],
        "scale": [1.0, 200, 200],
        "rotation_degrees": [0, 0, 0]
      },
      "color_linear_rgb": [5.319, 5.319, 5.714],
      "selected": false,
      "editable": true
    }
  ],
  "total": 1024
}
```

**示例**：查询所有Item 7
```javascript
const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_actors",
  args: {
    browser_session_id,
    item_id: 7,
    limit: 4096
  }
});

console.log(`Total slopes: ${result.total}`);
result.actors.forEach(actor => {
  console.log(`Actor ${actor.editor_id}: ${JSON.stringify(actor.transform)}`);
});
```

---

## 视图工具

### set_camera

**用途**：设置摄像机位置和目标，用于查看建模结果。

**参数**（新API）**：
```json
{
  "browser_session_id": "string",
  "position": [6400, 5000, 6400],
  "target": [6400, 6400, 6400]
}
```

**参数**（旧API兼容）**：
```json
{
  "browser_session_id": "string",
  "world_location_cm": [6400, 5000, 6400],
  "world_rotation_degrees": [-20, 45, 0]
}
```

**坐标系统**：
- X轴：东西向（正东）
- Y轴：上下向（正上）
- Z轴：南北向（正南）

**示例**：从东北方看地图中心的立方体
```javascript
const center = [6400, 6400, 6400];

// 方法1：使用position + target
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    position: [center[0] + 400, center[1] - 300, center[2] + 300],
    target: center
  }
});

// 方法2：使用旧API
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    world_location_cm: [center[0] + 400, center[1] - 300, center[2] + 300],
    world_rotation_degrees: [-20, 45, 0]
  }
});
```

---

## 高级操作工具

### copy_actors

**用途**：复制actor组（保留transform和属性）。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "actor_ids": [123, 124, 125]
}
```

**返回**：新的actor_id列表

---

### move_actors

**用途**：移动actor组到新位置（相对或绝对）。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "actor_ids": [123, 124, 125],
  "delta": [100, 0, 0]
}
```

---

### array_actors

**用途**：在阵列模式下复制actor组（x、y、z方向）。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "actor_ids": [123],
  "count": [3, 2, 1],
  "step": [200, 150, 0]
}
```

**效果**：在X、Y、Z方向各复制，间距为step

---

### mirror_actors

**用途**：镜像actor组。

**参数**：
```json
{
  "browser_session_id": "string",
  "map_session_id": "string",
  "expected_revision": 12345,
  "actor_ids": [123],
  "plane": "x",
  "plane_value": 6400
}
```

---

## 典型工作流

### 完整建模流程

```javascript
// 1️⃣ 初始化
const sessions = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_sessions",
  args: {}
});
const browser_session_id = sessions.sessions[0].browser_session_id;

const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});
const { map_session_id, revision } = state;

// 2️⃣ 定义几何
const placements = [...]; // 构建你的placement数组

// 3️⃣ 放置
const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision: revision,
    placements
  }
});

const actor_ids = result.placed_actors.map(a => a.editor_id);

// 4️⃣ 预览
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    position: [6400, 5000, 6400],
    target: [6400, 6400, 6400]
  }
});

// 5️⃣ 修改（如需要）
const newState = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});

await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "update_actors",
  args: {
    browser_session_id,
    map_session_id: newState.map_session_id,
    expected_revision: newState.revision,
    actor_ids,
    color_linear_rgb: [1.0, 2.0, 3.0]
  }
});
```

---

## 常见错误和解决

| 错误 | 原因 | 解决 |
|------|------|------|
| `"sessions": []` | 没有浏览器连接 | 打开Map Engine在浏览器中，等待配对 |
| `OUT_OF_BOUNDS` | 坐标超范围 | 检查location_cm和scale，确保都在[0,12800]范围 |
| `REVISION_CONFLICT` | 地图被修改 | 重新调用get_state获取新revision |
| `ACTOR_NOT_FREELY_PLACEABLE` | 物品不支持free_transform | 只用place_actors放置Item 7等free_transform物品 |
| `placement_errors` | 某些placement失败 | 检查返回的placement_errors数组，单个失败不影响其他 |

---

## 注意事项

✅ **要做**：
- 每次写入前获取最新revision
- 使用location_delta_cm移动多个actor（不要用location_cm）
- 分批处理超过4096个placement
- 处理placement_errors

❌ **不要做**：
- 硬编码session_id
- 使用过期revision（会REVISION_CONFLICT）
- 忘记转换sRGB颜色
- 一次性放置超过4096个actor

---

**返回建模指南**：需要快速上手？回到[prompt.md](./prompt.md)查看核心原理和示例。

