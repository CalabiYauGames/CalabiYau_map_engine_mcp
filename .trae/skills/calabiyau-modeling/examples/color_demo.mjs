/**
 * Example: Color conversion demonstration
 * Shows how to convert sRGB colors to linear RGB multipliers for Item 7
 */

// Helper function: sRGB to linear
function srgbToLinear(byte) {
  const c = byte / 255.0;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

// Item 7 base color (measured in-game)
const BASE = [0.188, 0.188, 0.175];

// Convert sRGB (0-255) to linear multipliers
function dye(srgbColor) {
  return srgbColor.map((val, i) => {
    const linear = srgbToLinear(val);
    const multiplier = linear / BASE[i];
    const maxMultiplier = 1.0 / BASE[i];
    return Math.min(multiplier, maxMultiplier);
  });
}

// Step 1: Get session
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

// Step 2: Define color palette (sRGB)
const colors = {
  red: [255, 0, 0],
  green: [0, 255, 0],
  blue: [0, 0, 255],
  yellow: [255, 255, 0],
  cyan: [0, 255, 255],
  magenta: [255, 0, 255],
  white: [255, 255, 255],
  orange: [255, 165, 0]
};

// Step 3: Create colored plates in a row
const startX = 6000;
const y = 6400;
const z = 6400;
const spacing = 200;
const plateSize = 150;

const placements = Object.entries(colors).map(([name, rgb], index) => {
  const linearColor = dye(rgb);
  
  console.log(`${name}: sRGB ${rgb} → linear ${linearColor.map(v => v.toFixed(3))}`);
  
  return {
    item_id: 7,
    transform: {
      location_cm: [startX + index * spacing, y, z],
      scale: [1.0, plateSize, plateSize],
      rotation_degrees: [0, 0, 0]
    },
    color_linear_rgb: linearColor
  };
});

// Step 4: Place the colored plates
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

console.log(`✓ Placed ${result.placed_actors.length} colored plates`);

// Step 5: Position camera to view the palette
await tools.run_mcp({
  server_name: "mcp_calabiyau-map-engine",
  tool_name: "set_camera",
  args: {
    browser_session_id,
    world_location_cm: [startX + 700, y - 500, z + 200],
    world_rotation_degrees: [-15, 30, 0]
  }
});

console.log("Color palette complete! Check browser to see the colors.");
