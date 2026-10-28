# CalabiYau Modeling Examples

这个目录包含了可执行的建模示例。每个示例展示了如何通过MCP直接进行特定类型的建模。

## 运行示例

所有示例都是JavaScript（`.mjs`）文件，可以在TraeCode中执行。

### 前提条件

1. Map Engine在浏览器中运行且已配对
2. AI代理有权限调用MCP工具

### 运行方式

在TraeCode中执行：

```javascript
// 将示例代码复制到TraeCode，执行即可
// 例如：执行examples/cube.mjs中的内容
```

或使用MCP执行工具（如果可用）：

```bash
lark-cli mcp exec mcp_calabiyau-map-engine examples/cube.mjs
```

---

## 示例列表

### 1. cube.mjs - 基础立方体

**目的**：演示最简单的建模工作流 - 创建一个立方体

**核心概念**：
- 立方体由6个面组成（每个面是一个Item 7 plate）
- 每个面的位置、大小、旋转计算
- 一次性batch placement

**输出**：6个actors组成的立方体

**学习点**：
- 如何定义Item 7的placement结构
- 如何使用transform（location_cm、scale、rotation_degrees）
- 如何调用place_actors批量放置
- 如何设置摄像机预览结果

**参考代码**：
```javascript
const placements = faces.map(f => ({
  item_id: 7,
  transform: {
    location_cm: f.loc,
    scale: [thickness, size, size],
    rotation_degrees: f.rot
  },
  color_linear_rgb: white
}));
```

---

### 2. slide.mjs - 弯曲滑滑梯

**目的**：演示如何用多个朝向连续变化的 Item 7 面片近似弯曲滑道。

**核心概念**：
- 用路径点定义滑道中心线
- 每段按相邻路径点计算位置、长度和方向
- 使用同一条曲线路径生成两侧护栏
- 需要更平滑的曲面时增加路径点

**输出**：分段滑道、护栏和若干支撑腿

---

### 3. sphere.mjs - 球体建模

**目的**：演示复杂曲面建模 - 创建一个球体

**核心概念**：
- 使用球坐标生成多个quad
- 曲面近似（subdivision细分）
- 批量处理超过4096个actor的情况

**输出**：256个actors组成的球体（16×16细分）

**学习点**：
- 如何生成算法化的几何（循环生成placement）
- 如何处理大量placement的batching
- 如何在每批之间更新revision
- 旋转计算（pitch/yaw）

**参数调整**：
- `subdivisions`：增加细分数 → 更光滑但更多actor
- `radius`：球体大小
- 建议范围：8-32（256-1024个actor）

**参考代码**：
```javascript
for (let lat = 0; lat < subdivisions; lat++) {
  for (let lon = 0; lon < subdivisions; lon++) {
    // 计算球坐标
    const phiMid = ...;
    const thetaMid = ...;
    // 转换为笛卡尔坐标
    const x = center[0] + radius * Math.cos(phiMid) * Math.cos(thetaMid);
    const y = center[1] + radius * Math.cos(phiMid) * Math.sin(thetaMid);
    const z = center[2] + radius * Math.sin(phiMid);
  }
}
```

---

### 3. color_demo.mjs - 颜色转换演示

**目的**：演示sRGB到linear RGB的颜色转换

**核心概念**：
- sRGB (0-255) → linear RGB multipliers转换
- Item 7的base color
- 完整的颜色调色板

**输出**：8个不同颜色的plates排成一行

**学习点**：
- 如何使用srgbToLinear()函数
- 如何使用dye()函数转换颜色
- MCP期望的颜色格式
- 常见颜色的linear值

**颜色参考**：
```
sRGB [255, 0, 0] (红) → linear [2.7, 0.0, 0.0]
sRGB [0, 255, 0] (绿) → linear [0.0, 4.8, 0.0]
sRGB [0, 0, 255] (蓝) → linear [0.0, 0.0, 5.7]
sRGB [255, 255, 255] (白) → linear [5.3, 5.3, 5.7]
```

**参考代码**：
```javascript
function srgbToLinear(byte) {
  const c = byte / 255.0;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

const BASE = [0.188, 0.188, 0.175];

function dye(srgbColor) {
  return srgbColor.map((val, i) => {
    const linear = srgbToLinear(val);
    const multiplier = linear / BASE[i];
    const maxMultiplier = 1.0 / BASE[i];
    return Math.min(multiplier, maxMultiplier);
  });
}
```

---

## 示例使用场景

### 场景1：快速原型

使用`cube.mjs`作为模板：
1. 复制cube.mjs的工作流
2. 修改center、size、color参数
3. 修改faces数组定义不同几何
4. 执行并预览

### 场景2：学习建模步骤

按照执行顺序理解：
1. **初始化**（list_sessions + get_state）
2. **定义几何**（构建placement数组）
3. **放置**（place_actors）
4. **预览**（set_camera）
5. **修改**（可选，update_actors）

### 场景3：算法几何

基于`sphere.mjs`模板创建其他参数化几何：
- **圆柱体**：沿圆周生成quad
- **锥体**：半径随高度变化
- **棋盘**：网格pattern
- **波形曲面**：高度场sampled

### 场景4：颜色处理

使用`color_demo.mjs`：
1. 理解sRGB → linear转换
2. 创建颜色调色板
3. 为几何体着色
4. 使用update_actors改变颜色

---

## 常见修改

### 改变立方体大小

在`cube.mjs`中：
```javascript
const size = 200;  // 改成300或100等
```

### 改变球体分辨率

在`sphere.mjs`中：
```javascript
const subdivisions = 16;  // 改成8(64个), 24(576个), 32(1024个)
```

### 改变位置

所有示例中：
```javascript
const center = [6400, 6400, 6400];  // 改成其他坐标
```

### 改变颜色

所有示例中：
```javascript
const white = [5.319149, 5.319149, 5.714286];  // 使用其他linear RGB值
```

或在`color_demo.mjs`中添加新颜色：
```javascript
const colors = {
  red: [255, 0, 0],
  // ... 添加更多颜色
  purple: [128, 0, 128]
};
```

---

## 故障排除

### 问题：执行后没看到任何东西

**原因**：
- Map Engine未打开或未配对
- 摄像机位置不对

**解决**：
- 确保Map Engine在浏览器中运行
- 执行`set_camera`调整视角
- 检查console输出是否有错误

### 问题：OUT_OF_BOUNDS错误

**原因**：坐标超出地图范围 [0, 12800]

**解决**：
- 检查center坐标
- 检查scale不会使actor超出边界
- 参考：actor的边界是 center ± (scale × local_extents)

### 问题：REVISION_CONFLICT错误

**原因**：地图在执行期间被修改

**解决**：
- 示例中已处理（每批前重新get_state）
- 或使用`expected_revision: "latest"`跳过检查

### 问题：颜色看起来奇怪

**原因**：sRGB → linear转换不对

**解决**：
- 运行color_demo.mjs验证转换
- 检查dye()函数的实现
- 参考mcp-reference.md中的颜色转换部分

---

## 扩展示例

### 创建自己的示例

模板：

```javascript
// 1️⃣ 初始化
const sessions = await tools.run_mcp({...}); // list_sessions
const browser_session_id = sessions.sessions[0].browser_session_id;

const state = await tools.run_mcp({...}); // get_state
const { map_session_id, revision } = state;

// 2️⃣ 定义几何
const placements = [
  { item_id: 7, transform: {...}, color_linear_rgb: [...] },
  // ... 更多placement
];

// 3️⃣ 放置
const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: { browser_session_id, map_session_id, expected_revision: revision, placements }
});

// 4️⃣ 预览
await tools.run_mcp({...}); // set_camera
```

### 使用Python辅助

参考[prompt.md](../prompt.md)中的几何算法：
- `Model.lathe()` - 旋转面
- `Model.ellipsoid()` - 椭球
- `Model.extrude()` - 挤出

---

## 快速参考

| 任务 | 示例 | 参考 |
|------|------|------|
| 创建简单shape | cube.mjs | prompt.md - Decision Tree |
| 曲面建模 | sphere.mjs | mcp-reference.md - place_actors |
| 着色 | color_demo.mjs | prompt.md - Color Conversion |
| 修改已有geometry | - | mcp-reference.md - update_actors |
| 删除geometry | - | mcp-reference.md - delete_actors |
| 查询已放置的actor | - | mcp-reference.md - list_actors |

---

**需要更多细节？** 查看：
- [prompt.md](../prompt.md) - 核心建模指南
- [mcp-reference.md](../mcp-reference.md) - MCP工具完整参考

