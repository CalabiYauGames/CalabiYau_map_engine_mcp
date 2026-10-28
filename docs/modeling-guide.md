# AI 建模指南：通过 MCP 工具使用 CalabiYau Map Engine

> **给 AI 看的核心文档**：你现在连接到浏览器端的 3D 地图编辑器，可以**直接通过 MCP 工具建模**，无需外部 Python 脚本。

## 🎯 快速上手

### 你能做什么

- 创建 3D 几何体（立方体、球体、复杂模型）
- 修改现有几何体（颜色、位置、缩放、旋转）
- 查询场景状态
- 控制相机视角
- 使用批量操作（复制、阵列、镜像）

### 基本工作流

```javascript
// 1️⃣ 获取会话信息
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

// 2️⃣ 建模：给 4 个顶点，工具自动算位置/缩放/旋转（单位 cm）
const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_quad",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision: revision,
    // 一个 2m × 2m、位于地图中心的竖直面，法向朝 +X
    // 顺序固定：[角1, 相邻角2, 相邻角3, 对角角4]
    vertices: [
      [6400, 6300, 6300],
      [6400, 6500, 6300],
      [6400, 6300, 6500],
      [6400, 6500, 6500]
    ],
    color_linear_rgb: [5.319, 5.319, 5.714]  // 线性 RGB，浅灰
  }
});

// 要一次放很多块就换 place_quads，quads 里每项是 {vertices, color_linear_rgb}，上限 4096

// 3️⃣ 调整相机预览
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    position: [6400, 5000, 6400],
    target: [6400, 6400, 6400]
  }
});
```

---

## ⚡ 核心原则

### 1. 物品 7 是你唯一的建模基元

**物品 7（斜坡/铁板/SM_PlaneType）** 是唯一应该用于 3D 建模的物品（除非用户明确要求其他物品）：

- 支持 **free_transform**（完整的浮点位置、缩放、旋转）
- 可以理解为 3D 软件中的**"三角形面"**或**"平板"**
- 可以放置为零厚度，形成平面
- 所有其他格子方块**不适合**精密建模

**策略**：除非用户点名要别的物品，否则永远用物品 7。

### 2. 旋转是必需的，不是可选的 ⚠️

**物品 7 的默认朝向是面向 +X 方向**（东），这几乎不会是你想要的朝向。建模时：

- **墙面**：需要设置 yaw（绕 Z 轴旋转）来控制朝向（东/南/西/北）
- **地板/天花板**：需要设置 roll（绕 X 轴旋转 ±90°）
- **斜坡**：需要设置 pitch（绕 Y 轴倾斜）
- **任意朝向**：需要根据法向量计算欧拉角

**不设置旋转 = 所有面片都朝向 +X = 错误的建模结果**

> 用 `place_quad` / `place_quads` 时不用自己管旋转：给出 4 个顶点，工具按面的法向自动算出 `rotation_degrees`。只有手写 `place_actors` 的 transform 时，才需要照下面的规则自己算。

详见"物品 7 的几何形状与旋转"章节的完整说明和示例。

### 3. 坐标系统

- **世界边界**：**动态获取**，从 `get_state` 返回的 `grid` 字段读取
  ```javascript
  const state = await tools.run_mcp({
    server_name: "mcp_calabiyau-map-engine",
    tool_name: "get_state",
    args: { browser_session_id }
  });
  const { grid } = state;              // 格子数，例如 { x: 128, y: 128, z: 128 }
  const worldMaxCm = [grid.x * 100, grid.y * 100, grid.z * 100];  // 0 .. 12800cm
  const centerCm = worldMaxCm.map(v => v / 2);                     // 地图中心（cm）
  ```
- **两种单位不要混**：`location_cm` 和 `place_quad` 的顶点是**厘米**；`cell`、`fill_box` 的 `min_cell`/`max_cell` 是**格子数**。越界会触发 `OUT_OF_BOUNDS`（cm 超出 `0..grid*100`，格子超出 `0..grid-1`）。

### 4. 物品 7 的几何形状与旋转

#### 基础几何

物品 7 的原始尺寸（从 `list_items` 查询的 `local_extents_cm`）：
- **X 轴（厚度）**：-25 至 0 cm（25cm 厚）
- **Y 轴（宽度）**：-50 至 +50 cm（100cm 宽）
- **Z 轴（高度）**：-50 至 +50 cm（100cm 高）

**枢轴点**：可见面的中心  
**法线方向**（面朝向）：本地 +X 轴（默认朝向 +X）  
**scale 参数**：`[厚度缩放倍数, 宽度缩放倍数, 高度缩放倍数]`

⚠️ **重要**：scale 是相对于原始尺寸的**缩放倍数**，不是绝对厘米值！
- `scale: [1.0, 1.0, 1.0]` → 厚度 25cm，宽度 100cm，高度 100cm
- `scale: [0.2, 2.0, 1.5]` → 厚度 5cm (25×0.2)，宽度 200cm (100×2.0)，高度 150cm (100×1.5)
- `scale: [0, 4.0, 3.0]` → 厚度 0cm，宽度 400cm，高度 300cm（零厚度平面）

⚠️ **面轴的单位是「米」**：宽度/高度写 `2.0` 就是 2m（200cm），写成 `200` 会得到 200m。手工拼 `place_actors` 时最常见的错误就是这里。

✅ **用 `place_quad` / `place_quads` 就不必算 scale**：工具按顶点自动推导，且板的厚度固定为 `0.0001cm`（近乎零厚，避免相邻面在棱上互相穿插）。

#### 旋转格式（虚幻引擎欧拉角）

使用 `rotation_degrees: [pitch, yaw, roll]`，单位为度：
- **Pitch**：绕 Y 轴旋转（上下倾斜）
- **Yaw**：绕 Z 轴旋转（左右转向）
- **Roll**：绕 X 轴旋转（翻滚）

#### ⚠️ 旋转是建模的核心 - 必须根据需求设置朝向！

物品 7 的**默认朝向**是面向 +X 方向（东），如果不设置旋转：
- 竖直平面会**平行于 YZ 平面**（南北墙）
- 水平平面需要旋转 90° 才能成为地板

**常见用途与旋转配置**：

| 想要实现的效果 | `rotation_degrees` 参数 | 说明 |
|-------------|----------------------|------|
| **东墙**（朝西，默认） | `[0, 0, 0]` | 面向 +X，平行于 YZ 平面 |
| **南墙**（朝北） | `[0, 90, 0]` | 绕 Z 轴旋转 90° |
| **西墙**（朝东） | `[0, 180, 0]` | 绕 Z 轴旋转 180° |
| **北墙**（朝南） | `[0, -90, 0]` | 绕 Z 轴旋转 -90° |
| **水平地板**（朝上） | `[0, 0, 90]` | 绕 X 轴旋转 90° |
| **水平天花板**（朝下） | `[0, 0, -90]` | 绕 X 轴旋转 -90° |
| **45° 斜坡**（向上） | `[45, 0, 0]` | 绕 Y 轴倾斜 45° |
| **朝向任意方向** | 计算法向量的欧拉角 | 见下方"朝向任意方向"章节 |

#### 朝向任意方向（根据法向量计算旋转）

如果你需要面片**朝向特定方向**（例如朝向某个点、沿着某条边），需要：

1. **计算目标法向量**（单位向量）
2. **将法向量转换为欧拉角 `[pitch, yaw, roll]`**

```javascript
// 示例：让平面朝向从 A 点到 B 点的方向
function calculate_rotation_to_face(from_pos, to_pos) {
  // 1. 计算方向向量
  const dir = [
    to_pos[0] - from_pos[0],
    to_pos[1] - from_pos[1],
    to_pos[2] - from_pos[2]
  ];
  
  // 2. 归一化
  const len = Math.sqrt(dir[0]**2 + dir[1]**2 + dir[2]**2);
  const norm = dir.map(v => v / len);
  
  // 3. 转换为欧拉角（虚幻引擎约定）
  const yaw = Math.atan2(norm[1], norm[0]) * 180 / Math.PI;
  const pitch = Math.asin(norm[2]) * 180 / Math.PI;
  const roll = 0;  // 通常不需要 roll
  
  return [pitch, yaw, roll];
}

// 示例：让平面朝向地图中心
const face_center = [6400, 6400, 6400];
const panel_pos = [6400, 5000, 6400];
const rotation = calculate_rotation_to_face(panel_pos, face_center);

await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    placements: [{
      item_id: 7,
      transform: {
        location_cm: panel_pos,
        scale: [0.0001, 2.0, 2.0],   // 近乎零厚、2m×2m
        rotation_degrees: rotation  // 朝向中心
      },
      color_linear_rgb: [5.319, 5.319, 5.714]
    }]
  }
});
```

#### 快速参考：常见建筑元素的旋转

```javascript
// 四周的墙（围成一个 8m × 8m 的房间，每面墙 8m 宽 × 3m 高）
const wallScale = [0.0001, 8.0, 3.0];   // [厚(可忽略), 宽 8m, 高 3m]
const room_walls = [
  { pos: [6400, 6000, 6500], rot: [0, 0, 0], scale: wallScale },    // 南墙（面向北）
  { pos: [6400, 6800, 6500], rot: [0, 180, 0], scale: wallScale }, // 北墙（面向南）
  { pos: [6000, 6400, 6500], rot: [0, 90, 0], scale: wallScale },  // 西墙（面向东）
  { pos: [6800, 6400, 6500], rot: [0, -90, 0], scale: wallScale }  // 东墙（面向西）
];

// 地板和屋顶（8m × 8m）
const floor = { pos: [6400, 6400, 6200], rot: [0, 0, 90], scale: [0.0001, 8.0, 8.0] };    // 朝上
const ceiling = { pos: [6400, 6400, 6600], rot: [0, 0, -90], scale: [0.0001, 8.0, 8.0] }; // 朝下

// 斜坡（从南到北，4m 宽 × 4m 长，向上倾斜 30°）
const ramp = { pos: [6400, 6400, 6400], rot: [30, 0, 0], scale: [0.0001, 4.0, 4.0] };
```


### 5. 颜色转换（sRGB → 线性 RGB）

MCP 需要**线性 RGB 倍增值**，而不是 sRGB 颜色。

```python
def srgb_to_linear(byte: float) -> float:
    """将 sRGB 字节值转换为线性值"""
    c = byte / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

# 物品 7 的基础颜色（实测）
BASE = (0.188, 0.188, 0.175)

def dye(srgb_color: tuple[int, int, int]) -> list[float]:
    """将 sRGB (0-255) 转换为物品 7 的线性倍增值"""
    return [
        round(min(srgb_to_linear(srgb_color[i]) / BASE[i], 1.0 / BASE[i]), 6)
        for i in range(3)
    ]
```

**示例**：
- sRGB 白色 (255, 255, 255) → `[5.319149, 5.319149, 5.714286]`
- sRGB 灰色 (180, 180, 200) → `[4.061, 4.061, 5.068]`

---

## 🔧 常用 MCP 工具

### 会话管理

| 工具 | 用途 | 何时调用 |
|-----|------|---------|
| `list_sessions` | 列出已连接的浏览器会话 | 开始建模前，获取 `browser_session_id` |
| `get_state` | 获取地图状态和修订号 | 每次写操作前，获取 `map_session_id` 和 `expected_revision` |

### 核心建模工具

**三种建模工具集，用途不同：**

#### 🎨 顶点建模工具（推荐用于精密建模）

**最简单的精密建模方式**——只需给出顶点坐标，MCP 自动计算所有 transform 参数：

| 工具 | 用途 | 参数要点 |
|-----|------|---------|
| `place_quads` | **批量放置矩形面**（最多 4096） | `quads: [{vertices: [[x,y,z], ...], color_linear_rgb?: [r,g,b]}]` |
| `place_quad` | 放置单个矩形面 | `vertices: [[x,y,z], [x,y,z], [x,y,z], [x,y,z]]`, `color_linear_rgb?: [r,g,b]` |

**顶点顺序**：工具会自己找对边、判定哪两条是邻边，**四种顺序都接受**，建议沿矩形轮廓顺序书写便于阅读。

**几何要求**：四个点必须共面且构成矩形（对边等长、邻边垂直），否则返回 `INVALID_GEOMETRY`。板的厚度由工具固定为 `0.0001cm`，不需要也不能通过参数调整。

**示例**：
```javascript
// 放置一个 2m×1.5m 的矩形面（此处按轮廓顺序书写）
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_quad",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    vertices: [
      [-100, -75, 0],   // corner 1
      [100, -75, 0],    // corner 2 (adjacent to 1, along X)
      [-100, 75, 0],    // corner 3 (adjacent to 1, along Y)
      [100, 75, 0]      // corner 4 (opposite to 1)
    ],
    color_linear_rgb: [5.319, 5.319, 5.714]  // 可选，线性 RGB 乘数值
  }
});

// 批量放置多个面
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_quads",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    quads: [
      {
        vertices: [[-100, -75, 0], [100, -75, 0], [-100, 75, 0], [100, 75, 0]],
        color_linear_rgb: [5.319, 5.319, 5.714]
      },
      {
        vertices: [[0, 0, 0], [100, 0, 0], [0, 0, 100], [100, 0, 100]],
        color_linear_rgb: [4.0, 4.0, 4.5]
      }
      // ... 最多 4096 个
    ]
  }
});
```

**为什么推荐使用顶点建模**：
- ✅ AI 只需关注几何形状，无需计算 scale/rotation 参数
- ✅ 自动处理任意朝向、任意角度的矩形面
- ✅ 一次批量放置最多 4096 个面，效率高
- ✅ 自动验证几何有效性（检查对边相等、相邻边垂直）

#### 精密建模工具（物品 7 + 浮点变换）

用于**需要直接控制 transform 参数的场景**（通常不需要，推荐使用上述顶点建模工具）：

| 工具 | 用途 | 参数要点 |
|-----|------|---------|
| `place_actors` | **批量放置物品**（最多 4096） | `item_id: 7`, `transform: {location_cm, scale, rotation_degrees}`, `color_linear_rgb` |
| `update_actors` | 修改现有物品 | `actor_ids`, `changes: {color_linear_rgb?, transform?, ...}` |
| `delete_actors` | 删除物品 | `actor_ids` |
| `list_actors` | 查询已放置的物品 | 分页查询，用于验证或批量操作 |

#### 体素建筑工具（方块 + 格子坐标）

用于**快速建造房屋、墙体、地形**等格子结构：

| 工具 | 用途 | 参数要点 |
|-----|------|---------|
| `fill_box` | 批量填充长方体（最多 4096 格） | `mode: "solid"/"shell"/"walls"`, `item_id` 必须是 `build_kind: voxel` |
| `fill_sphere` | 批量填充球体（半径 0-32） | `mode: "solid"/"hollow"`, `radius`, `item_id` 必须是 `build_kind: voxel` |
| `delete_region` | 批量删除区域内的格子方块 | `min_cell`, `max_cell` |

**如何选择**：
- **精密建模**：斜面、雕塑、精细模型 → 用 `place_actors` + 物品 7
- **体素建筑**：房屋框架、墙体、地板、方块地形 → 用 `fill_box`/`fill_sphere` + 物品 1/2/4/5

### 批量操作工具

| 工具 | 用途 | 参数 |
|-----|------|------|
| `copy_actors` | 复制物品并偏移 | `actor_ids`, `offset: [x, y, z]` |
| `array_actors` | 线性阵列复制 | `actor_ids`, `direction`, `spacing`, `count` |
| `mirror_actors` | 镜像复制 | `actor_ids`, `axis`, `origin` |
| `polar_array` | 环形阵列 | `actor_ids`, `center`, `axis`, `angle`, `count` |

### 相机与视觉

| 工具 | 用途 |
|-----|------|
| `set_camera` | 设置相机位置和目标点 |
| `get_camera` | 获取当前相机状态 |
| `capture_view` | 截取当前视口为本地图片 |

### 查询与空间分析

| 工具 | 用途 |
|-----|------|
| `list_items` | 查询可用物品目录（含 `build_kind`、`free_transform` 等） |
| `query_space` | 按包围盒查询区域内已占用的格子 |
| `describe_region` | 区域统计、包围盒、占用率和 ASCII 地图 |
| `find_free_space` | 查找可容纳指定尺寸的空闲位置 |

---

## 📐 常见建模模式

### 方法 A：体素建筑（快速建造方块结构）

#### 1. 快速建造房屋框架（使用 `fill_box`）

```javascript
// 建一个 10×10×3 米的房间框架（墙体，无地板和屋顶）
const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});
const room_center_cell = [Math.floor(state.grid.x / 2), Math.floor(state.grid.y / 2), 0];

await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "fill_box",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    min_cell: [room_center_cell[0] - 5, room_center_cell[1] - 5, room_center_cell[2]],
    max_cell: [room_center_cell[0] + 5, room_center_cell[1] + 5, room_center_cell[2] + 3],
    item_id: 1,  // 物品 1（基础方块）
    mode: "walls",  // 只建墙体，不建地板和屋顶
    color_linear_rgb: [4.5, 4.0, 3.5]
  }
});
```

#### 2. 球形地形（使用 `fill_sphere`）

```javascript
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "fill_sphere",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    center_cell: [64, 64, 64],  // 地图中心
    radius: 10,  // 半径 10 格（10 米）
    item_id: 4,  // 物品 4（石头）
    mode: "solid",
    color_linear_rgb: [3.0, 3.0, 3.2]
  }
});
```

### 方法 B：精密建模（使用物品 7 + 浮点变换）

#### 1. 单个平面（墙面/地面）- 重点：设置旋转朝向

```javascript
// ❌ 错误示例一：忘记设置旋转，平面只是默认朝向（+X）
{
  "item_id": 7,
  "transform": {
    "location_cm": [6400, 6400, 6400],
    "scale": [0.0001, 4.0, 3.0],   // 厚(近乎0)、宽 4m、高 3m
    "rotation_degrees": [0, 0, 0]  // 默认朝向 +X
  },
  "color_linear_rgb": [5.319, 5.319, 5.714]
}

// ❌ 错误示例二：把米当成倍数写
// scale: [0.01, 400, 300] 得到的是 0.25cm 厚、400m 宽、300m 高的巨板

// ✅ 正确示例：根据需求设置旋转（面轴以米为单位）
// 东墙（朝西）
{
  "item_id": 7,
  "transform": {
    "location_cm": [6400, 6400, 6400],
    "scale": [0.0001, 4.0, 3.0],
    "rotation_degrees": [0, 0, 0]  // 默认朝向 +X
  },
  "color_linear_rgb": [5.319, 5.319, 5.714]
}

// 南墙（朝北）
{
  "item_id": 7,
  "transform": {
    "location_cm": [6400, 6400, 6400],
    "scale": [0.0001, 4.0, 3.0],
    "rotation_degrees": [0, 90, 0]  // 绕 Z 轴旋转 90°
  },
  "color_linear_rgb": [5.319, 5.319, 5.714]
}

// 水平地板（朝上）
{
  "item_id": 7,
  "transform": {
    "location_cm": [6400, 6400, 6300],
    "scale": [0.0001, 4.0, 4.0],
    "rotation_degrees": [0, 0, 90]  // 绕 X 轴旋转 90°
  },
  "color_linear_rgb": [5.319, 5.319, 5.714]
}

// 45° 斜坡（向上）
{
  "item_id": 7,
  "transform": {
    "location_cm": [6400, 6400, 6400],
    "scale": [0.0001, 4.0, 4.0],
    "rotation_degrees": [45, 0, 0]  // 绕 Y 轴倾斜 45°
  },
  "color_linear_rgb": [5.319, 5.319, 5.714]
}
```

#### 2. 立方体（6 个面）

使用 6 个物品 7 面板，每个面对应一个方向：

```javascript
const center = [6400, 6400, 6400];
const sizeCm = 200;                 // 2m 立方体：位置用 cm
const sizeM = sizeCm / 100;         // 2.0：scale 的面轴用米

const cube_faces = [
  // 前面 (+X)
  { location_cm: [center[0] + sizeCm/2, center[1], center[2]], scale: [0, sizeM, sizeM], rotation_degrees: [0, 0, 0] },
  // 后面 (-X)
  { location_cm: [center[0] - sizeCm/2, center[1], center[2]], scale: [0, sizeM, sizeM], rotation_degrees: [0, 180, 0] },
  // 右侧 (+Y)
  { location_cm: [center[0], center[1] + sizeCm/2, center[2]], scale: [0, sizeM, sizeM], rotation_degrees: [0, 90, 0] },
  // 左侧 (-Y)
  { location_cm: [center[0], center[1] - sizeCm/2, center[2]], scale: [0, sizeM, sizeM], rotation_degrees: [0, -90, 0] },
  // 顶部 (+Z)
  { location_cm: [center[0], center[1], center[2] + sizeCm/2], scale: [0, sizeM, sizeM], rotation_degrees: [0, 0, 90] },
  // 底部 (-Z)
  { location_cm: [center[0], center[1], center[2] - sizeCm/2], scale: [0, sizeM, sizeM], rotation_degrees: [0, 0, -90] }
];

// 转换为 placements
const placements = cube_faces.map(face => ({
  item_id: 7,
  transform: face,
  color_linear_rgb: [5.319, 5.319, 5.714]
}));
```

#### 3. 弯曲滑滑梯（用 place_quads 铺连续四边形）

弯曲滑道不要用 `place_actors` 手工算每一段的旋转：只要沿路径给出四条边，`place_quads` 会自动求出法向、缩放和欧拉角。做法是把路径中心线向左右各偏移半个滑道宽度，得到两条栅线，相邻两个采样点之间就是一个四边形。

```javascript
// 左右栅线：沿路径的水平法向偏移半宽
function rails(path, halfWidth) {
  return path.map((point, i) => {
    const prev = path[Math.max(0, i - 1)];
    const next = path[Math.min(path.length - 1, i + 1)];
    const dx = next[0] - prev[0];
    const dy = next[1] - prev[1];
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len;   // 水平面内的横向法向
    const ny = dx / len;
    return {
      left:  [point[0] + nx * halfWidth, point[1] + ny * halfWidth, point[2]],
      right: [point[0] - nx * halfWidth, point[1] - ny * halfWidth, point[2]]
    };
  });
}

// 相邻采样点之间一块板；四点沿轮廓顺序给出即可（工具自己配对邻边）
function slide_quads(path, halfWidth, colorLinearRgb) {
  const r = rails(path, halfWidth);
  const quads = [];
  for (let i = 0; i < r.length - 1; i++) {
    quads.push({
      vertices: [r[i].left, r[i + 1].left, r[i].right, r[i + 1].right],
      color_linear_rgb: colorLinearRgb
    });
  }
  return quads;
}

const slideColor = [5.319, 0.65, 0.08];
const railColor = [0.25, 0.25, 0.28];
const center = [state.grid.x * 50, state.grid.y * 50, state.grid.z * 50];  // 地图中心（cm）

// 中心线：高处直段 → 圆弧下降 → 低处直段，单位 cm
const path = [
  [center[0] - 1400, center[1] - 1000, center[2] + 1200],
  [center[0] - 1100, center[1] - 1000, center[2] + 1200],
  [center[0] - 800,  center[1] - 1000, center[2] + 1100],
  [center[0] - 550,  center[1] - 1000, center[2] + 900],
  [center[0] - 350,  center[1] - 1000, center[2] + 600],
  [center[0] - 200,  center[1] - 1000, center[2] + 200],
  [center[0] - 100,  center[1] - 1000, center[2] - 200],
  [center[0],        center[1] - 1000, center[2] - 500],
  [center[0] + 300,  center[1] - 1000, center[2] - 700],
  [center[0] + 700,  center[1] - 1000, center[2] - 700]
];

const quads = [
  // 滑道本体：宽 260cm
  ...slide_quads(path, 130, slideColor),
  // 两侧扶手的下沿：沿同一路径抬高 35cm、横向再外扩 145cm
  ...slide_quads(path.map(([x, y, z]) => [x, y + 145, z + 35]), 10, railColor),
  ...slide_quads(path.map(([x, y, z]) => [x, y - 145, z + 35]), 10, railColor)
];

const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_quads",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    quads
  }
});
```

**关键点**：

- 相邻采样点越密，弯道越圆滑，但板数随之增加；先用 `path` 的采样密度控制效果，再看总数是否可接受。
- 四点顺序不限，但必须共面且构成矩形：对边长度相差超过 0.5cm、或邻边不垂直，都会返回 `INVALID_GEOMETRY`。
- 想让滑道有可见厚度（侧壁），沿路径两侧再放一圈竖直条带；平板本身默认近乎零厚。
- 护栏要是沿路径偏移后的独立栅线，只在起点终点插竖杆是不对的。

#### 4. 球体

球面同样可以用 `place_quads` 按经纬线铺：横向 N 段、纵向 M 段，每格一个四边形，顶点取球面坐标 `[cx + r·sinθ·cosφ, cy + r·sinθ·sinφ, cz + r·cosθ]`。细分数 16~32 即可平衡观感与板数。

**注意**：极点附近的四边形会退化成三角形，要单独用更小的块收口，否则会留孔；简单球形地形直接用 `fill_sphere` 体素工具更省事。

---

## ⚠️ 常见错误与解决方案

| 错误代码 | 原因 | 解决方法 |
|---------|------|---------|
| `OUT_OF_BOUNDS` | 坐标超出世界边界 | `location_cm` 应在 `0 .. grid*100` cm 内；`cell` 应在 `0 .. grid-1` 格内 |
| `REVISION_CONFLICT` | 修订号过期（其他操作已更新地图） | 重新调用 `get_state` 获取新的 `expected_revision` |
| `CELL_OCCUPIED` | 格子已被占用 | 使用 `query_space` 检查空间，或设置 `overwrite: true` |
| `EDIT_MODE_REQUIRED` | 编辑器不在编辑模式 | 提示用户在浏览器中进入编辑模式 |
| `ACTOR_NOT_FOUND` | actor_id 不存在 | 使用 `list_actors` 验证 ID |
| `ITEM_NOT_PLACEABLE` | 尝试放置不可放置的物品 | 检查 `list_items` 的 `placeable` 字段 |
| `INVALID_GEOMETRY` | 四个顶点不共面 / 不构成矩形 | 重算顶点：对边等长、邻边垂直；`place_quads` 会带 `index` 指出是哪一块 |

### 处理 `placement_errors`

`place_actors` 返回的 `placement_errors` 数组包含每个失败放置的详细信息：

```javascript
if (result.placement_errors && result.placement_errors.length > 0) {
  console.log("部分放置失败：");
  result.placement_errors.forEach(err => {
    console.log(`- 索引 ${err.index}: ${err.error} (格子 ${err.cell})`);
  });
}
```

---

## 🚫 避免这些错误

❌ **不要做**：
- 使用格子方块（物品 1-6, 8+）进行**精密建模**（用于体素建筑可以）
- **忘记设置 `rotation_degrees`** - 物品 7 的默认朝向是 +X，几乎总是需要旋转！
- 忘记 sRGB → 线性 RGB 颜色转换
- 在边界外放置几何体（`get_state().grid` 是格子数，cm 上限是 `grid*100`）
- 使用硬编码的会话 ID（始终先调用 `list_sessions`）
- 重用过期的 `expected_revision`（每次写操作前都要 `get_state`）
- 把手工算 `rotation_degrees` 当成首选方案（能用 `place_quad` 就用它）

✅ **应该做**：
- 始终使用物品 7 进行**精密建模**（除非明确要求其他物品）
- **根据需求计算并设置正确的旋转角度**（参考"物品 7 的几何形状与旋转"章节）
- 使用 `fill_box`/`fill_sphere` + 物品 1/2/4/5 进行**体素建筑**
- 批量放置（每次调用最多 4096 个）以提高性能
- 处理 `placement_errors` 中的失败情况
- 使用 `location_delta_cm`（而非 `location_cm`）批量移动多个 actor
- 在复杂模型前先用 `dry_run` 验证

---

## 📊 决策树

```
用户想建模？
├─ 需要格子级别的建筑（房屋、墙体、地形）
│  └─ 使用体素工具：fill_box/fill_sphere + 物品 1/2/4/5
├─ 需要精密几何（斜面、曲面、雕塑）
│  ├─ 简单几何体（< 100 个面）
│  │  └─ 给出 4 个顶点 → place_quad
│  ├─ 复杂几何体（100-4000 个面）
│  │  └─ 批量生成 vertices → place_quads（一次最多 4096 块）
│  ├─ 非常复杂（> 4000 个面）
│  │  └─ 分多次 place_quads 调用，或改用 import_model 体素化
│  └─ 需要非平面的变换（厚度、自定义 scale/rotation）
│     └─ 才退回 place_actors + 手工 transform
```

---

## 🎓 完整示例：用物品 7 建一个雕塑

```javascript
// 建一个抽象几何雕塑：相交的平面
const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});
const center = [state.grid.x * 50, state.grid.y * 50, state.grid.z * 50];  // 地图中心（cm）
const faceScale = [0.0001, 4.0, 4.0];  // [厚(近乎0), 宽 4m, 高 4m]

const placements = [
  // 竖直平面 1
  {
    item_id: 7,
    transform: {
      location_cm: center,
      scale: faceScale,
      rotation_degrees: [0, 0, 0]
    },
    color_linear_rgb: [5.319, 2.5, 2.5]  // 红色
  },
  // 竖直平面 2（旋转 90 度）
  {
    item_id: 7,
    transform: {
      location_cm: center,
      scale: faceScale,
      rotation_degrees: [0, 90, 0]  // 绕 Z 轴旋转
    },
    color_linear_rgb: [2.5, 5.319, 2.5]  // 绿色
  },
  // 水平平面（倾斜 45 度）
  {
    item_id: 7,
    transform: {
      location_cm: center,
      scale: faceScale,
      rotation_degrees: [45, 0, 90]  // 倾斜
    },
    color_linear_rgb: [2.5, 2.5, 5.714]  // 蓝色
  }
];

await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    placements
  }
});

// 调整相机查看
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    position: [center[0] - 800, center[1] - 800, center[2] + 600],
    target: center
  }
});
```

---

## 🧱 实战案例：用 112 块斜坡建正八面体

> 这是一次真实迭代的完整记录：从 248 块 → 160 块 → 112 块，前两版的通病都是「面本身对，但棱衔接不好」。下面把踩过的坑和最终方法分开列出，新来的 AI 可以直接照抄成功例、避开失败例。
>
> 参考实现：项目根目录 `build_octahedron.py`（生成顶点）+ `prepare_quads.py`（排序/平移/复验）。
> 本地脚本只负责**算顶点和自检**，最终仍然通过 `place_quads` 提交，所以不违背「直接通过 MCP 建模」的原则。

### 案例几何（先算清楚再动手）

正八面体总高 1000cm（10 格），8 个**全等**等腰三角面：

| 量 | 值 |
|---|---|
| 赤道顶点距中心 | 707.11cm |
| 面底边（赤道棱） | 1000cm |
| 面高（底边中点到顶点） | 707.11cm |
| 面腰（极棱） | 866.03cm |
| 底角（赤道顶点处） | 54.74°，cot = 0.7071 |
| 顶角（极点处） | 70.53°，cot = 0.3536 |

**关键事实：六个顶点角全是锐角。** 后面所有规则都由这一条推出来。

### ❌ 失败例 1：沿棱铺一整条宽板，顶点处必然飞出（248 块）

想把每个面用最大的板铺满：横带铺到边，再沿两条腰各铺一条 220cm 宽的宽条带。结果每个锐角顶点都伸出三角面之外，形成明显的悬空薄片。

原因是可以算出来的：沿棱方向宽 `w` 的条带，它靠面内的内侧角点要留在楔形里，条带起点必须满足

```
s ≥ w · cot(θ)          θ = 该端顶点的夹角
```

`w = 220`、底角 54.74° 时需要 `s ≥ 155.6cm`，而这条棱总共才 866cm；`w = 220` 在极点端更是需要 `s ≥ 77.8cm`。以前"目测差不多"就下结论，于是必然越界。

### ❌ 失败例 2：自检规则定成「至少 3 个角在面内」

最初的校验只要求每块板**大部分**落在面内。这个判据太松：一块板可以 3 个角在里面、第 4 个角飞出去 60cm，检查照样通过——但玩家一眼就看见飞边。

**正确判据：4 个角全部在面内**，容差最多 1cm（容差是给浮点误差留的，不是给设计误差留的）。

### ❌ 失败例 3：横带做成等宽矩形铺满整面

想让每面 8 条矩形覆盖，每条宽度都取「该带中心处的三角形宽度」。结果：

- 带的**底边**处三角形更窄，矩形两侧冒出三角形之外 → 又是沿腰的飞边；
- 带的**顶边**处三角形更窄，矩形盖不住 → 每条带顶部留一道锯齿缺口，棱边仍然透光。

**正确做法：宽度取该带顶边处的三角形宽度。**

```python
hw = half_base * (1.0 - w1 / height)   # w1 = 该带的顶边高度
```

这样板永远在面内（窄于底边、等于顶边），代价是带底留下缺口 —— 缺口交给沿棱的条带去盖。

### ❌ 失败例 4：所有条带都放在同一平面 / 板太厚

- 同一面上两块板严格共面且重叠 → 深度冲突，远看是闪烁的斑块；
- 之前 `place_quad` 内写死 `thickness_cm = 1.0`，相邻两个面的板在棱上并排，各自凸出 1cm，棱线上出现一条 1cm 的 V 形槽，正对强光时是一条黑线。

**现在 `quad_to_transform` 的最薄厚度已改为 `0.0001cm`（scale x = 4e-6）**，棱缝的 V 形槽基本消失（需要重启 MCP 服务后生效）。仍需注意：若是手工给 `scale`，不要写成 `[1, ...]`（那是 25cm 厚），薄板请写 `[0.0001, ...]` 或更小。

### ❌ 失败例 5：以为「面之间」需要专门填缝

一开始想给 12 条棱都铺条带。实际上 4 条**赤道棱**不需要：第 0 条横带的下边正好压在这条棱上，两个面的横带已经是边对边。只有 2 条**极棱**（从赤道顶点到极点）才需要条带，因为横带在那边是阶梯状收窄的。

多做 4 条棱的条带，只增加了板数和穿插，没有改善画面。

### ✅ 成功例：横带 + 渐变条带链（112 块）

每个面 14 块：6 条横带（大斜坡）+ 每条极棱 4 段渐变条带（2 条棱 × 4 = 8 块）。

1. **横带**：把面高切成 7 份，最上一份宽度为 0，实际铺 6 条；每条是一整块大矩形（最宽约 857 × 101cm），宽度取带顶边处的面宽。面中央只用大斜坡，块数少且绝对共面。
2. **极棱条带**：外长边**精确压在棱线上**（相邻两面的条带于是边对边，缝隙为零），向面内延伸 `w`。按 `s ≥ w·cot θ` 分段收短：

   | 段 | 宽度 | 位置 | 作用 |
   |---|---|---|---|
   | 1 | 22cm | 赤道顶点端 | 小斜坡，塞进最尖锐的角 |
   | 2 | 40cm | 过渡 | 中斜坡 |
   | 3 | 70cm | 棱中段 | 大斜坡，盖住横带 58cm 的锯齿缺口 |
   | 4 | 18cm | 极点端 | 小斜坡 |

   **这就是"大斜坡 / 小斜坡"的分工：面中央和棱中段用大板（质量、块数双赢），越靠近锐角顶点越要换窄板。**
3. **抬升错开**：同一面两条棱的条带分别抬 0.6/1.2 与 0.9/1.5cm，避免极点附近两块板完全重合导致闪烁。
4. **上色**：8 个面用 8 种颜色，验证「每个面都贴对、没有面接错顶点」。

最终结果：`112` 块，4 角越界面数 `0`，面内最大未覆盖距离 `17.0cm`（4cm 采样，折算实际约 13cm），全部集中在 4 个赤道顶点和 2 个极点，正常视距下不可见。

### 可直接复用的规则

1. **先算角度，再排版。** 任何「板贴着棱」的方案，先量该端顶点夹角，用 `w·cot θ` 决定这条板最早能从哪里开始。
2. **板的宽度向上取、向下不取。** 宽度取上边界的可用宽度，板就永远不会越界。
3. **锐角附近一律换窄板。** 复杂度只增加在角上，中间区域保持大板。
4. **外长边贴棱，内边留余量。** 想消除面与面之间的缝，必须让相邻两面的板在棱线上"共用同一条线"，而不是各自往里收一点。
5. **自检 4 角在面内**（容差 1cm），不是 3 角。
6. **用距离场量化缝隙。** 在面内按网格采样 → 判断点是否被任一板覆盖 → 倒角距离变换，输出「离最近一块板的最远距离（cm）」。`0` 才算无缝；这个数比肉眼看截图可靠得多。
7. **四面交于一点时留 1~2cm 的实心余量**，不要指望用板拼出绝对的数学点；7cm 的小孔在掠射角度下是可接受的，再想缩小只会指数级增加板数。

### 自检代码骨架（可直接搬到自己脚本里）

```python
def inside_face(point, apex, e1, e2, normal, tol=1.0):
    """板的 4 个角必须全部满足：到面平面的距离 <= 3cm，且在 3 条边的内侧 >= -tol。"""
    if abs(dot(sub(point, apex), normal)) > 3.0:
        return False
    for a, b, other in ((apex, e1, e2), (e1, e2, apex), (e2, apex, e1)):
        inward = unit(cross(normal, sub(b, a)))      # 指向面内的法线
        if dot(inward, sub(other, a)) < 0:
            inward = [-x for x in inward]
        if dot(sub(point, a), inward) < -tol:
            return False
    return True


def cot_angle(vertex, a, b):
    """vertex 处的角余切，直接用于决定条带起点。"""
    angle = math.acos(clamp(dot(unit(sub(a, vertex)), unit(sub(b, vertex)))))
    return 1.0 / math.tan(angle)
```

生成完先跑这两个检查，打印「越界角点数」和「最大未覆盖距离」，两个数字都符合预期再 `place_quads`。**先验证，再上地图**，比放上去之后靠截图找毛病快得多。

---

## 🔗 更多资源

### 本项目文档

- **`docs/surface-modeling.md`** — **把外部 3D 模型（PMX / GLB）还原成面板**：平面域生长、mipmap 取色、
  双层薄壳导致的毛刺、`place_quads` 角点顺序与批量颜色字段等 15 个坑。做人物/有机曲面必读。
- **`docs/protocol.md`** - 桥接消息和错误约定

### 项目目录下的 Skill 文档

如果你能访问项目文件系统，还可以查阅：

- **`.trae/skills/calabiyau-modeling/prompt.md`** - 核心建模原则和快速工作流
- **`.trae/skills/calabiyau-modeling/mcp-reference.md`** - 完整的 MCP 工具参考（705 行，包含所有参数、返回值、错误处理）
- **`.trae/skills/calabiyau-modeling/examples/`** - 可运行的代码示例（cube.mjs、sphere.mjs、color_demo.mjs）
- **`.trae/skills/calabiyau-modeling/GUIDE.md`** - 文档导航决策树

### MCP 协议细节

- **`docs/protocol.md`** - 桥接消息和错误约定（`docs/surface-modeling.md` 也在同目录）
- **`docs/issue-1-*.md`、`docs/tests-acceptance-review.md`** - 历史存档，只记录当时状态，不要据此建模

---

## 📞 获取帮助

**遇到错误或需要 MCP 工具的详细说明？**

1. **先读本文档**的"常见错误与解决方案"部分
2. **查阅项目 Skill 文档**（如果能访问文件系统）：`.trae/skills/calabiyau-modeling/mcp-reference.md` 包含完整的参数规范和错误代码
3. **使用 `describe_region` 和 `list_actors`** 调试空间占用问题
4. **使用 `capture_view`** 截图验证建模结果

**不要猜测参数 - 先读文档！**
