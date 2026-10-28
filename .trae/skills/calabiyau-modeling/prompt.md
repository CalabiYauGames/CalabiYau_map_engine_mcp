# CalabiYau Map Engine 3D Modeling Guide

You are connected to a **browser-based 3D map editor** via MCP tools. This guide shows you how to model directly using MCP, without external Python scripts.

## 📖 Documentation Structure

- **THIS FILE** (`prompt.md`) - Quick start guide and core principles
- **[mcp-reference.md](mcp-reference.md)** - **Complete MCP tool reference** (detailed parameters, examples, error handling)
- **[examples/](examples/)** - Runnable code examples

**When you need detailed MCP tool usage** (parameters, return values, error codes), **open and read [mcp-reference.md](mcp-reference.md)**. Don't guess - the reference has the authoritative specs.

## Core Principle: Item 7 is Your Only Modeling Primitive

**Item 7 (斜坡/铁板)** is the ONLY item you should use for 3D modeling, unless the user explicitly requests otherwise.

- It supports **free_transform** (full floating-point position, scale, rotation)
- Think of it as a **"triangle face"** in 3D modeling software
- Can be placed with 0 thickness, forming flat surfaces
- All other grid blocks are NOT suitable for modeling

## Quick Workflow

1. **Get session info**
   ```
   list_sessions → get browser_session_id
   get_state → get map_session_id + expected_revision
   ```

2. **Model with plates** (Item 7)
   ```
   place_actors with:
   - item_id: 7
   - transform: {location_cm, scale, rotation_degrees}
   - color_linear_rgb: [r, g, b]  // linear RGB multipliers
   ```

3. **Batch placement**: up to 4096 actors per call

## Essential Technical Facts

### Map Boundaries
- World bounds: `[0, 0, 0]` to `[12800, 12800, 12800]` cm (128×128×128 grid cells, 100cm/cell)
- **DO NOT** exceed these bounds - OUT_OF_BOUNDS error will occur

### Item 7 Geometry (measured)
At scale 1, a plate occupies:
- X: -0.25 to 0 (thickness direction)
- Y: -0.5 to +0.5 (width)
- Z: -0.5 to +0.5 (height)

**Pivot**: center of visible face  
**Normal**: local +X axis  
**Scale**: meters, so `[t, w, h]` = thickness t, width w, height h

### Color Conversion (sRGB → Linear)
MCP expects **linear RGB multipliers**, not sRGB colors.

```python
def srgb_to_linear(byte: float) -> float:
    c = byte / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

# Slope base color (measured in-game)
BASE = (0.188, 0.188, 0.175)

def dye(srgb_color: tuple[int, int, int]) -> list[float]:
    """Convert sRGB (0-255) to linear multipliers for Item 7"""
    return [
        round(min(srgb_to_linear(srgb_color[i]) / BASE[i], 1.0 / BASE[i]), 6)
        for i in range(3)
    ]
```

**Example**: For sRGB white (255, 255, 255):
```python
dye((255, 255, 255)) → [5.319149, 5.319149, 5.714286]
```

### Rotation
Use **Unreal Engine Euler angles** `[pitch, yaw, roll]` in degrees.

## Common Patterns

### Single Quad
```json
{
  "item_id": 7,
  "transform": {
    "location_cm": [6400, 6400, 6400],
    "scale": [0.01, 100, 100],
    "rotation_degrees": [0, 0, 0]
  },
  "color_linear_rgb": [5.319149, 5.319149, 5.714286]
}
```

### Box (12 quads)
Model a box by placing 6 faces (front/back/left/right/top/bottom), each split into 2 thin triangles if needed, or use 12 overlapping plates.

### Curved Surfaces
- Use many small quads to approximate smooth surfaces
- Lathe, sphere subdivision, extrusion patterns work well
- Batch them with `place_actors`

## MCP Tools You'll Use

| Tool | Purpose | Details |
|------|---------|---------|
| `list_sessions` | Get browser_session_id | [→ Reference](mcp-reference.md#list_sessions) |
| `get_state` | Get map_session_id, expected_revision | [→ Reference](mcp-reference.md#get_state) |
| `list_items` | Query item catalog | [→ Reference](mcp-reference.md#list_items) |
| `place_actors` | **Main modeling tool**: batch place up to 4096 plates | [→ Reference](mcp-reference.md#place_actors) |
| `update_actors` | Modify existing actors | [→ Reference](mcp-reference.md#update_actors) |
| `delete_actors` | Remove actors by actor_id | [→ Reference](mcp-reference.md#delete_actors) |
| `list_actors` | Query placed actors | [→ Reference](mcp-reference.md#list_actors) |
| `set_camera` | Position viewport | [→ Reference](mcp-reference.md#set_camera) |
| `copy_actors`, `move_actors`, `array_actors`, `mirror_actors` | Advanced operations | [→ Reference](mcp-reference.md#高级操作工具) |

**Need parameter details, return values, or error codes?** → Read [mcp-reference.md](mcp-reference.md)

## Constraints & Error Handling

- **4096 placement limit**: If you need more, call `place_actors` multiple times
- **REVISION_CONFLICT**: Re-fetch `get_state` and use new `expected_revision`
- **OUT_OF_BOUNDS**: Check your coordinates are within [0, 12800]cm
- **Component density**: Item 7 doesn't occupy strict grid cells, but too many in one area may cause density errors

## Avoid These Mistakes

❌ **Using `import_model`** - frontend feature is incomplete  
❌ **Placing other items (non-Item 7)** - unless user asks explicitly  
❌ **Coordinates > 12800 cm** - map boundary violation  
❌ **Forgetting sRGB → linear conversion** - colors will look wrong  
❌ **Writing external Python scripts** - you can model directly via MCP!

## Decision Tree

```
User wants to model something?
├─ Simple geometry (< 100 faces)
│  └─ Generate placement JSON inline → call place_actors
├─ Complex geometry (100-4000 faces)
│  └─ Batch generate placements → call place_actors (or multiple calls)
└─ Very complex (> 4000 faces)
   └─ Split into multiple place_actors calls, or simplify geometry
```

## Example: Create a Simple Cube

```javascript
// 1. Get session
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
const { map_session_id, expected_revision } = state;

// 2. Define cube (6 faces × 2 cm size, centered at 6400, 6400, 6400)
const center = [6400, 6400, 6400];
const size = 100; // 1m cube
const white = [5.319, 5.319, 5.714];

const faces = [
  { loc: [center[0] + size/2, center[1], center[2]], rot: [0, 0, 0] },    // +X
  { loc: [center[0] - size/2, center[1], center[2]], rot: [0, 180, 0] },  // -X
  { loc: [center[0], center[1], center[2] + size/2], rot: [0, 0, 90] },   // +Z
  { loc: [center[0], center[1], center[2] - size/2], rot: [0, 0, -90] },  // -Z
  { loc: [center[0], center[1] + size/2, center[2]], rot: [90, 0, 0] },   // +Y
  { loc: [center[0], center[1] - size/2, center[2]], rot: [-90, 0, 0] }   // -Y
];

const placements = faces.map(f => ({
  item_id: 7,
  transform: {
    location_cm: f.loc,
    scale: [0.5, size, size],
    rotation_degrees: f.rot
  },
  color_linear_rgb: white
}));

// 3. Place the cube
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision,
    placements
  }
});
```

## Reference Examples
For complex geometry helpers (lathe, ellipsoid, extrusion), see the runnable
examples in [examples/](examples/).

They demonstrate:
- `srgb_to_linear()` and `dye()` functions
- Single plate placement
- Surface of revolution
- Subdivided sphere
- Coordinate transforms and normal calculations

---

**Remember**: You are modeling directly in the browser through MCP. No need for separate scripts. Item 7 is your "triangle face" primitive. Happy modeling!
