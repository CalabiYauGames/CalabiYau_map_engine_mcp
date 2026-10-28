# CalabiYau Modeling Skill

这个Skill让AI能够直接通过MCP工具进行3D建模，无需编写和执行外部Python脚本。

## 目标

- **直接建模**：AI通过MCP工具直接操作Map Engine，无需中间脚本
- **快速上手**：新的AI接入后立即理解如何使用Item 7进行建模
- **自动触发**：当AI遇到建模相关任务时，自动加载建模指南

## 文件结构

```
.trae/skills/calabiyau-modeling/
├── skill.json          # Skill配置（触发关键词、auto_load设置）
├── prompt.md           # 核心建模指南（快速上手、核心原则）
├── mcp-reference.md    # **MCP工具完整参考**（所有参数、返回值、错误处理）
├── README.md           # 本文件（面向用户的说明和部署指南）
└── examples/           # 可执行示例代码
    ├── cube.mjs        # 基础立方体示例
    ├── sphere.mjs      # 球体建模（曲面近似）
    └── color_demo.mjs  # 颜色转换演示
```

## 核心原则

**Item 7（斜坡/铁板）是唯一的建模原语**

- Item 7支持free_transform（任意位置、缩放、旋转）
- 类似3D建模软件中的"三角面"
- 可设置为0厚度，形成平面
- 其他地图方块不适合建模用途

## AI如何使用

当AI遇到以下关键词时，Skill会自动加载：
- `model`, `modeling`, `建模`
- `3d`, `geometry`
- `calabiyau`, `map engine`
- `place_actors`

加载后，AI将获得：
1. Item 7的技术规格（几何、坐标、颜色转换）
2. MCP工具的正确使用方式
3. 常见建模模式（单个面、立方体、球体）
4. 决策树和错误处理指南
5. 可执行的示例代码

## 部署说明

### 1. 确保Map Engine运行

```bash
cd CalabiYau_Map_Engine/CalabiYau_Map_Engine
npm run dev
```

在浏览器中打开 `http://localhost:5173`

### 2. 确保MCP Server运行

MCP server应该已经通过TraeCode的MCP系统自动连接。检查MCP连接状态：

```javascript
// 在TraeCode中执行
const sessions = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_sessions",
  args: {}
});

console.log(sessions);
```

如果看到浏览器会话信息，说明连接正常。

### 3. Skill自动生效

Skill文件已经放置在 `.trae/skills/calabiyau-modeling/`，当AI遇到建模相关任务时会自动加载。

## 示例使用场景

### 场景1：创建简单几何体

**用户**："帮我在地图中心创建一个立方体"

**AI行为**：
1. 自动加载calabiyau-modeling skill
2. 理解需要使用Item 7构建立方体（6个面）
3. 通过MCP调用 `place_actors` 直接创建
4. 无需编写Python脚本

### 场景2：复杂曲面建模

**用户**："创建一个球体，半径2米"

**AI行为**：
1. 使用球坐标细分算法生成多个quad
2. 计算每个quad的位置、旋转、大小
3. 批量调用 `place_actors`（最多4096个/次）
4. 如果超过限制，自动分批处理

### 场景3：颜色处理

**用户**："把这些方块改成红色"

**AI行为**：
1. 知道需要sRGB → linear RGB转换
2. 应用正确的颜色转换公式
3. 使用 `update_actors` 修改颜色

## 技术细节

### 坐标系统
- 地图范围：[0, 0, 0] 到 [12800, 12800, 12800] cm
- 128×128×128网格，每格100cm
- 中心点：[6400, 6400, 6400]

### Item 7几何
- 在scale=1时占据：X: -0.25..0, Y: -0.5..+0.5, Z: -0.5..+0.5
- 法向量：局部+X轴
- 支点：可见面中心

### 颜色转换
sRGB (0-255) → Linear RGB multipliers

```python
def srgb_to_linear(byte):
    c = byte / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

BASE = (0.188, 0.188, 0.175)  # Item 7 base color

def dye(srgb_color):
    return [srgb_to_linear(srgb_color[i]) / BASE[i] for i in range(3)]
```

### MCP工具清单

| 工具 | 用途 |
|------|------|
| `list_sessions` | 获取browser_session_id |
| `get_state` | 获取map_session_id和expected_revision |
| `place_actors` | **核心建模工具**：批量放置最多4096个actor |
| `update_actors` | 修改已存在actor的属性 |
| `delete_actors` | 删除actor |
| `set_camera` | 设置视角位置 |
| `capture_view` | 截图当前视图 |

## 限制和注意事项

- **4096限制**：单次`place_actors`最多4096个，超过需分批
- **地图边界**：坐标必须在[0, 12800]cm范围内
- **revision冲突**：如遇REVISION_CONFLICT，重新获取expected_revision
- **避免import_model**：前端的模型导入功能尚不完善

## 故障排除

### 问题：AI仍然想写Python脚本

**解决**：确保Skill的触发关键词包含用户的请求关键词。如果需要，手动提醒AI："请直接使用MCP工具建模，不要写Python脚本"。

### 问题：颜色不正确

**解决**：检查是否正确应用了sRGB→linear转换，并除以BASE颜色。参考`examples/color_demo.mjs`。

### 问题：超出地图边界

**解决**：确保所有坐标在[0, 12800]cm范围内。可以先检查几何体的边界框，再放置。

### 问题：revision冲突

**解决**：在每次`place_actors`前重新获取`expected_revision`。如果分批放置，每批前都要更新。

## 参考资源

- **完整建模指南**：[docs/modeling-guide.md](../../../docs/modeling-guide.md)
- **表面建模实战**：[docs/surface-modeling.md](../../../docs/surface-modeling.md)
- **MCP 工具完整参考**：`mcp-reference.md`（AI 自动加载）

## 下一步

Skill已配置完成，AI现在可以：
- ✓ 自动识别建模任务
- ✓ 直接通过MCP操作Map Engine
- ✓ 正确使用Item 7进行建模
- ✓ 处理颜色、坐标、旋转等技术细节
- ✓ 批量处理大量几何体

**开始使用**：向AI发送建模请求即可，例如："在地图中心创建一个红色立方体"。
