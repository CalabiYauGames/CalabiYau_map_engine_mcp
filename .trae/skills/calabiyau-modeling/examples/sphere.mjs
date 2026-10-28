/**
 * Example: Create a sphere using subdivided quads
 * Demonstrates curved surface approximation with Item 7
 */

// Step 1: Get session
const sessions = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "list_sessions",
  args: {}
});

if (!sessions.sessions || sessions.sessions.length === 0) {
  throw new Error("No active browser session. Open Map Engine in browser first.");
}

const browser_session_id = sessions.sessions[0].browser_session_id;
const state = await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "get_state",
  args: { browser_session_id }
});

const { map_session_id, expected_revision } = state;

// Step 2: Generate sphere geometry
const center = [6400, 6400, 6400];
const radius = 150; // 1.5m radius
const subdivisions = 16; // lat/lon divisions
const thickness = 0.5; // thin plates

// Color: light blue
const lightBlue = [2.5, 3.0, 5.0];

const placements = [];

// Generate sphere using spherical coordinates
for (let lat = 0; lat < subdivisions; lat++) {
  for (let lon = 0; lon < subdivisions; lon++) {
    const phi1 = (Math.PI * lat) / subdivisions - Math.PI / 2;
    const phi2 = (Math.PI * (lat + 1)) / subdivisions - Math.PI / 2;
    const theta1 = (2 * Math.PI * lon) / subdivisions;
    const theta2 = (2 * Math.PI * (lon + 1)) / subdivisions;
    
    // Center of this quad
    const phiMid = (phi1 + phi2) / 2;
    const thetaMid = (theta1 + theta2) / 2;
    
    const x = center[0] + radius * Math.cos(phiMid) * Math.cos(thetaMid);
    const y = center[1] + radius * Math.cos(phiMid) * Math.sin(thetaMid);
    const z = center[2] + radius * Math.sin(phiMid);
    
    // Calculate rotation to point outward (normal)
    const yaw = (thetaMid * 180 / Math.PI);
    const pitch = (phiMid * 180 / Math.PI);
    
    // Quad size (approximation)
    const quadSize = (2 * Math.PI * radius) / subdivisions;
    
    placements.push({
      item_id: 7,
      transform: {
        location_cm: [x, y, z],
        scale: [thickness, quadSize * 0.8, quadSize * 0.8],
        rotation_degrees: [pitch, yaw, 0]
      },
      color_linear_rgb: lightBlue
    });
  }
}

console.log(`Placing ${placements.length} plates to form a sphere...`);

// Step 3: Place sphere (may need batching if > 4096)
if (placements.length <= 4096) {
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
  
  console.log(`✓ Placed ${result.placed_actors.length} actors`);
} else {
  console.log("⚠ Too many faces, batching into multiple calls...");
  
  for (let i = 0; i < placements.length; i += 4000) {
    const batch = placements.slice(i, i + 4000);
    
    // Re-fetch state for each batch
    const currentState = await tools.run_mcp({
      server_name: "mcp_calabiyau-map-engine",
      tool_name: "get_state",
      args: { browser_session_id }
    });
    
    await tools.run_mcp({
      server_name: "mcp_calabiyau-map-engine",
      tool_name: "place_actors",
      args: {
        browser_session_id,
        map_session_id: currentState.map_session_id,
        expected_revision: currentState.expected_revision,
        placements: batch
      }
    });
    
    console.log(`✓ Batch ${Math.floor(i / 4000) + 1} placed`);
  }
}

// Step 4: Position camera
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    world_location_cm: [center[0] + 400, center[1] - 400, center[2] + 300],
    world_rotation_degrees: [-25, 45, 0]
  }
});

console.log("Sphere complete! Check browser.");
