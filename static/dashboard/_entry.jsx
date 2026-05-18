// Bundle entry — concatenates the V1 dashboard in load order. Each source
// file is wrapped in its own IIFE so local `const` declarations don't collide.
//
// V2 files are NOT imported here. They're runtime-fetched + Babel-transformed
// by templates/dashboard.html only when localStorage.poybot_v2_lab === '1'.
// See memory: project_v1_vs_v2_terminal.md ("V1 = source of truth ; V2 = lab
// gated OFF, ne pas migrer") and ADR-PMK-014.1.

// api-client.js sets up window.LiveStore + window.PoybotAPI. Must run first.
import './api-client.js';
// Shared atoms, design tokens, categoryRisk helper.
import './dashboard-components.jsx';
// All tab implementations.
import './dashboard-tabs.jsx';
// Shell + nav + keyboard shortcuts. Mounts ReactDOM at the bottom.
import './dashboard-app.jsx';
