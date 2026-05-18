// Bundle entry — concatenates the V1 dashboard in load order. Each source
// file is wrapped in its own IIFE so local `const` declarations don't collide.
//
// V2 was removed 2026-05-19 (WG-A6). The Wallet Graph is now V3 ("univers")
// in static/dashboard/walletgraph_v3/, imported below before dashboard-tabs.jsx
// so window.WG_V3.{BackgroundAmbient,SelectionPanel,WalletGraphV3} are bound
// before the WalletGraph wrapper renders.

// api-client.js sets up window.LiveStore + window.PoybotAPI. Must run first.
import './api-client.js';
// Shared atoms, design tokens, categoryRisk helper.
import './dashboard-components.jsx';

// ─── Wallet Graph V3 (universe-style) ────────────────────────────────────────
// Order matters: SelectionPanel + BackgroundAmbient (siblings, no deps), then
// Graph (composes both via window.WG_V3.*).
import './walletgraph_v3/BackgroundAmbient.jsx';
import './walletgraph_v3/SelectionPanel.jsx';
import './walletgraph_v3/Graph.jsx';

// All tab implementations.
import './dashboard-tabs.jsx';
// Shell + nav + keyboard shortcuts. Mounts ReactDOM at the bottom.
import './dashboard-app.jsx';
