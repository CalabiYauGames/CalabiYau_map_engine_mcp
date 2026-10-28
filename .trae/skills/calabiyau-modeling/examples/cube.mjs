/**
 * Example: Create a simple cube using Item 7 plates
 * Demonstrates the basic modeling workflow with MCP
 */

// Step 1: Get session info
const sessions = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_sessions",
  args: {}
});

if (!sessions.sessions || sessions.sessions.length === 0) {
  throw new Error("No active browser session found. Please open Map Engine in browser first.");
}

const browser_session_id = sessions.sessions[0].browser_session_id;

const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});

const { map_session_id, expected_revision } = state;

console.log(`Connected to session: ${map_session_id}, revision: ${expected_revision}`);

// Step 2: Define cube geometry
const center = [6400, 6400, 6400]; // Map center
const size = 200; // 2 meter cube
const thickness = 1.0; // 1cm thick plates

// Color: White in linear RGB (for Item 7)
const white = [5.319149, 5.319149, 5.714286];

// Six faces of a cube
const faces = [
  { loc: [center[0] + size/2, center[1], center[2]], rot: [0, 0, 0], label: "+X" },
  { loc: [center[0] - size/2, center[1], center[2]], rot: [0, 180, 0], label: "-X" },
  { loc: [center[0], center[1] + size/2, center[2]], rot: [90, 0, 0], label: "+Y" },
  { loc: [center[0], center[1] - size/2, center[2]], rot: [-90, 0, 0], label: "-Y" },
  { loc: [center[0], center[1], center[2] + size/2], rot: [0, 0, 90], label: "+Z" },
  { loc: [center[0], center[1], center[2] - size/2], rot: [0, 0, -90], label: "-Z" }
];

const placements = faces.map(f => ({
  item_id: 7, // Item 7 = slope/plate
  transform: {
    location_cm: f.loc,
    scale: [thickness, size, size], // [thickness, width, height]
    rotation_degrees: f.rot
  },
  color_linear_rgb: white
}));

console.log(`Placing ${placements.length} plates to form a cube...`);

// Step 3: Place the cube
const result = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "place_actors",
  args: {
    browser_session_id,
    map_session_id,
    expected_revision,
    placements
  }
});

console.log("Placement result:", result);

if (result.placement_errors && result.placement_errors.length > 0) {
  console.error("Some placements failed:", result.placement_errors);
} else {
  console.log(`✓ Successfully placed ${result.placed_actors.length} actors`);
}

// Step 4: Position camera to view the cube
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    world_location_cm: [center[0] + 500, center[1] - 500, center[2] + 300],
    world_rotation_degrees: [-20, 45, 0]
  }
});

console.log("Camera positioned. Check the browser to see your cube!");
