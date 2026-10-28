/**
 * Example: Create a curved slide from oriented Item 7 panels.
 * The slide surface is approximated by short tangent-aligned segments.
 */

function rotationForDirection(direction) {
  const [dx, dy, dz] = direction;
  const horizontal = Math.hypot(dx, dy);
  const yaw = Math.atan2(dy, dx) * 180 / Math.PI;
  const pitch = Math.atan2(dz, horizontal) * 180 / Math.PI;
  return [pitch, yaw, 90];
}

function addSegment(placements, start, end, width_cm, thickness_cm, color) {
  const direction = [end[0] - start[0], end[1] - start[1], end[2] - start[2]];
  const length_cm = Math.hypot(...direction);
  const unitDir = direction.map(v => v / length_cm);
  const center = start.map((value, index) => (value + end[index]) / 2);

  // Item 7 原始尺寸: 厚度 25cm, 宽度 100cm, 高度 100cm
  // scale 是相对于原始尺寸的缩放倍数
  placements.push({
    item_id: 7,
    transform: {
      location_cm: center,
      scale: [thickness_cm / 25, width_cm / 100, length_cm / 100],
      rotation_degrees: rotationForDirection(unitDir)
    },
    color_linear_rgb: color
  });
}

const sessions = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_sessions",
  args: {}
});

if (!sessions.sessions || sessions.sessions.length === 0) {
  throw new Error("No active browser session found.");
}

const browser_session_id = sessions.sessions[0].browser_session_id;
const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});

const grid = state.grid;
const center = grid.min.map((value, index) => (value + grid.max[index]) / 2);
const slideColor = [5.319, 0.65, 0.08];
const railColor = [0.25, 0.25, 0.28];
const placements = [];

// Add more points for a smoother curve. Coordinates are centimeters relative to the current map center.
const path = [
  [center[0] - 1400, center[1] - 1000, center[2] + 1200],
  [center[0] - 1100, center[1] - 1000, center[2] + 1200],
  [center[0] - 800, center[1] - 1000, center[2] + 1100],
  [center[0] - 550, center[1] - 1000, center[2] + 900],
  [center[0] - 350, center[1] - 1000, center[2] + 600],
  [center[0] - 200, center[1] - 1000, center[2] + 200],
  [center[0] - 100, center[1] - 1000, center[2] - 200],
  [center[0], center[1] - 1000, center[2] - 500],
  [center[0] + 300, center[1] - 1000, center[2] - 700],
  [center[0] + 700, center[1] - 1000, center[2] - 700]
];

for (let i = 0; i < path.length - 1; i++) {
  addSegment(placements, path[i], path[i + 1], 200, 5, slideColor);  // 宽 200cm, 厚 5cm
}

// Duplicate the path on both sides to form raised rails.
for (const side of [-1, 1]) {
  const railPath = path.map(([x, y, z]) => [x, y + side * 110, z + 20]);
  for (let i = 0; i < railPath.length - 1; i++) {
    addSegment(placements, railPath[i], railPath[i + 1], 15, 8, railColor);  // 宽 15cm, 厚 8cm
  }
}

// Add a few vertical supports instead of one support per segment.
for (const [x, y, z] of [path[0], path[3], path[6], path[path.length - 1]]) {
  const legHeight = z - 100;
  placements.push({
    item_id: 7,
    transform: {
      location_cm: [x, y, 100 + legHeight / 2],
      scale: [30 / 25, 30 / 100, legHeight / 100],  // 方形截面 30cm×30cm
      rotation_degrees: [0, 0, 0]
    },
    color_linear_rgb: railColor
  });
}

const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id: state.map_session_id,
    expected_revision: state.revision,
    placements
  }
});

console.log("Slide placement result:", result);
