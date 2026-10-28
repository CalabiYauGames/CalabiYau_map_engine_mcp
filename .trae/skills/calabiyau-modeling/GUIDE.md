# CalabiYau建模Skill - 使用指南

## 📚 文档导航

```
您的建模问题或任务
    ↓
┌─────────────────────────────────────────────────────┐
│  "我想创建一个3D模型"                                 │
│  "怎样在Map Engine中放置物体"                         │
│  "需要用MCP做什么"                                   │
└─────────────────────────────────────────────────────┘
    ↓
    ├─→ 【快速理解】需要了解核心概念？
    │        └─→ 读 [prompt.md](./prompt.md)
    │           - Item 7是什么
    │           - 快速工作流（3步）
    │           - 坐标系统、颜色转换
    │           - 决策树（选择策略）
    │
    ├─→ 【看代码示例】想看实际工作代码？
    │        └─→ 查看 [examples/](./examples/)
    │           - cube.mjs - 立方体（最简单）
    │           - sphere.mjs - 球体（复杂）
    │           - color_demo.mjs - 颜色（学习转换）
    │
    └─→ 【查工具参数】需要具体MCP参数和返回值？
             └─→ 读 [mcp-reference.md](./mcp-reference.md)
                - place_actors 详细参数
                - get_state 返回字段
                - 错误处理（OUT_OF_BOUNDS等）
                - 完整工作流示例
```

---

## 🎯 快速决策树

### Q: 我是第一次接触这个系统

**→ 按顺序读：**
1. [prompt.md](./prompt.md) 开头（5分钟理解概念）
2. [examples/cube.mjs](./examples/cube.mjs)（理解代码结构）
3. 根据需要查[mcp-reference.md](./mcp-reference.md)

---

### Q: 我需要创建一个简单几何体（立方体、盒子等）

**→ 使用这个流程：**
1. 复制 [examples/cube.mjs](./examples/cube.mjs)
2. 修改 `center` 和 `size` 参数
3. 修改 `faces` 数组定义你的几何
4. 运行 → 预览 → 完成

**→ 如果需要细节：**
- 参数说明 → [mcp-reference.md - place_actors](./mcp-reference.md#place_actors)
- 旋转计算 → [prompt.md - Rotation](./prompt.md#rotation)
- 颜色转换 → [prompt.md - Color Conversion](./prompt.md#color-conversion-srgb--linear)

---

### Q: 我需要创建复杂曲面或参数化几何

**→ 使用这个流程：**
1. 查看 [examples/sphere.mjs](./examples/sphere.mjs)
2. 理解球坐标 → 笛卡尔坐标的转换
3. 适配到你的几何算法
4. 如果超过4096个，参考示例中的batching

**→ 关键问题：**
- 超过4096个actor怎么办？ → [examples/README.md - sphere.mjs](./examples/README.md#2-spheremjs---球体建模)
- 计算placement数组 → [mcp-reference.md - place_actors](./mcp-reference.md#place_actors)

---

### Q: 颜色看起来不对

**→ 问题诊断：**
1. 运行 [examples/color_demo.mjs](./examples/color_demo.mjs)
2. 对比输出中的sRGB → linear转换
3. 检查你的转换函数

**→ 查阅资源：**
- 转换公式 → [prompt.md - Color Conversion](./prompt.md#color-conversion-srgb--linear)
- Python实现参考 → [examples/color_demo.mjs](./examples/color_demo.mjs)

---

### Q: 出现错误（OUT_OF_BOUNDS, REVISION_CONFLICT等）

**→ 查看：**
- [mcp-reference.md - 常见错误和解决](./mcp-reference.md#常见错误和解决)
- 按错误类型找到解决方案

---

### Q: 我需要修改已有的geometry

**→ 使用 update_actors：**
- [mcp-reference.md - update_actors](./mcp-reference.md#update_actors)
- 改颜色、位置、大小、旋转

**→ 例子：**
```javascript
// 改颜色
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "update_actors",
  args: {
    browser_session_id, map_session_id, expected_revision,
    actor_ids: [123, 124, 125],
    color_linear_rgb: [5.0, 2.5, 1.0]
  }
});
```

---

### Q: 我需要删除或查询geometry

**→ 使用对应工具：**
- 删除 → [mcp-reference.md - delete_actors](./mcp-reference.md#delete_actors)
- 查询 → [mcp-reference.md - list_actors](./mcp-reference.md#list_actors)
- 查物品属性 → [mcp-reference.md - list_items](./mcp-reference.md#list_items)

---

## 📍 文件速览

| 文件 | 用途 | 何时读 |
|------|------|--------|
| **prompt.md** | 核心原则、快速工作流、决策树 | 🟢 首先读这个 |
| **mcp-reference.md** | 每个MCP工具的完整参数、返回值、错误 | 🟡 需要具体细节时 |
| **examples/README.md** | 每个示例的解释和修改方式 | 🟢 运行示例前读 |
| **examples/cube.mjs** | 简单立方体示例 | 🟢 学习基础代码 |
| **examples/sphere.mjs** | 复杂球体示例 | 🟡 学习batching和算法 |
| **examples/color_demo.mjs** | 颜色转换示例 | 🟡 调试颜色问题 |

🟢 = 一定要读  
🟡 = 需要时读  

---

## 🔄 典型工作流

### 工作流1：快速创建一个立方体

```
┌─ 打开examples/cube.mjs
├─ 理解其中的5个步骤
├─ 复制代码到你的AI对话/代码执行环境
├─ 运行 → 看到Map Engine中出现立方体
└─ 完成 ✓
```

**时间**：5分钟  
**文档**：examples/cube.mjs → mcp-reference.md (如需查细节)

---

### 工作流2：创建自己的参数化几何

```
┌─ 读[prompt.md](./prompt.md)理解Item 7和坐标
├─ 阅读[examples/sphere.mjs](./examples/sphere.mjs)学习batching
├─ 设计你的几何算法
├─ 编写生成placement数组的代码
├─ 查[mcp-reference.md - place_actors](./mcp-reference.md#place_actors)确保参数正确
├─ 运行 → 调试 → 完成
└─ (可选) 使用[update_actors](./mcp-reference.md#update_actors)修改
```

**时间**：15-30分钟  
**文档**：prompt.md → sphere.mjs → mcp-reference.md

---

### 工作流3：调试颜色问题

```
┌─ 运行[examples/color_demo.mjs](./examples/color_demo.mjs)
├─ 查看console中的转换结果
├─ 对比你用的颜色值
├─ 查[prompt.md - Color Conversion](./prompt.md#color-conversion-srgb--linear)或[mcp-reference.md](./mcp-reference.md)
├─ 修正转换公式
└─ 重新运行 → 完成 ✓
```

**时间**：5-10分钟  
**文档**：color_demo.mjs → prompt.md

---

## 🚨 常见陷阱

❌ **不要**：
- 硬编码session_id（每次都重新list_sessions）
- 用过期的revision（每次写入前重新get_state）
- 忘记sRGB→linear转换（颜色会错）
- 一次放4096+个actor（分批处理）
- 使用import_model（功能不完善）

✅ **要做**：
- 每次写入前fetch最新revision
- 处理placement_errors数组
- 使用location_delta_cm移动多个actor
- 分批处理大量geometry
- 参考[mcp-reference.md - 注意事项](./mcp-reference.md#注意事项)

---

## 🔗 快速链接

**学习核心**：[prompt.md](./prompt.md#core-principle-item-7-is-your-only-modeling-primitive)  
**查看代码**：[examples/](./examples/)  
**查工具参数**：[mcp-reference.md](./mcp-reference.md)  
**理解示例**：[examples/README.md](./examples/README.md)

---

## ❓ 还有问题？

1. **概念不清**？ → 读[prompt.md](./prompt.md)
2. **参数不对**？ → 查[mcp-reference.md](./mcp-reference.md)
3. **代码示例**？ → 看[examples/](./examples/)
4. **工具有问题**？ → 查[mcp-reference.md - 常见错误](./mcp-reference.md#常见错误和解决)

---

**现在就开始**：
1. 打开 [prompt.md](./prompt.md)
2. 或复制 [examples/cube.mjs](./examples/cube.mjs) 立即运行

Happy modeling! 🎨
