#!/usr/bin/env node
// ──────────────────────────────────────────────────────────────────────────────
// Polymarket bot dashboard — JSX precompile pipeline (PLAN-UIA-001, ADR-014.7).
//
// Goal: replace Babel-in-browser with a single precompiled bundle so the
// operator dashboard cold-starts in <1s instead of 3–5s.
//
// V1 source-of-truth files are bundled. V2 files are NOT bundled — they're
// runtime-fetched + Babel-transformed by templates/dashboard.html only when
// localStorage.poybot_v2_lab === '1'. Keeps V2 truly lab-only per memory
// project_v1_vs_v2_terminal.md.
//
// Usage:
//   node scripts/build_dashboard.mjs            # one-shot build
//   node scripts/build_dashboard.mjs --watch    # watch for changes
//   NODE_ENV=development node scripts/build_dashboard.mjs   # non-minified
// ──────────────────────────────────────────────────────────────────────────────

import * as esbuild from 'esbuild';
import { dirname, resolve } from 'path';
import { mkdirSync, existsSync, readFileSync, writeFileSync } from 'fs';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');
const SRC = resolve(ROOT, 'static/dashboard');
const OUT_DIR = resolve(SRC, 'dist');
const OUT_FILE = resolve(OUT_DIR, 'dashboard.bundle.js');

mkdirSync(OUT_DIR, { recursive: true });

// ── Synthesize an entry that concatenates the four V1 files in load order ───
// Each file uses its own IIFE wrapper for namespace isolation. We mirror the
// legacy templates/dashboard.html load-order so behaviour is identical.
const ENTRY_PATH = resolve(SRC, '_entry.jsx');
if (!existsSync(ENTRY_PATH)) {
  console.error('[build_dashboard] missing entry:', ENTRY_PATH);
  process.exit(1);
}

const isWatch = process.argv.includes('--watch');
const isDev = process.env.NODE_ENV === 'development';

const buildOpts = {
  entryPoints: [ENTRY_PATH],
  bundle: true,
  outfile: OUT_FILE,
  loader: { '.js': 'jsx', '.jsx': 'jsx' },
  jsx: 'transform',
  jsxFactory: 'React.createElement',
  jsxFragment: 'React.Fragment',
  target: ['es2020'],
  format: 'iife',
  globalName: 'PoybotDashboard',
  minify: !isDev,
  sourcemap: true,
  platform: 'browser',
  // React + ReactDOM come from the CDN <script> tags as window.React /
  // window.ReactDOM. We don't bundle them.
  inject: [],
  define: {
    'process.env.NODE_ENV': JSON.stringify(isDev ? 'development' : 'production'),
  },
  banner: {
    js: `// Poybot dashboard bundle — V1 source of truth. V2 lab files loaded separately. Built ${new Date().toISOString()}`,
  },
  logLevel: 'info',
};

const reportSize = () => {
  try {
    const bytes = readFileSync(OUT_FILE).length;
    const kb = (bytes / 1024).toFixed(1);
    console.log(`[build_dashboard] bundle: ${OUT_FILE} (${kb} KB)`);
  } catch (e) {
    console.warn('[build_dashboard] could not stat bundle:', e.message);
  }
};

if (isWatch) {
  const ctx = await esbuild.context(buildOpts);
  await ctx.watch();
  console.log('[build_dashboard] watching ' + SRC);
} else {
  const t0 = Date.now();
  const result = await esbuild.build(buildOpts);
  const dt = Date.now() - t0;
  console.log(`[build_dashboard] ✓ built in ${dt}ms`);
  reportSize();
  if (result.warnings.length) {
    console.warn('[build_dashboard] warnings:');
    for (const w of result.warnings) console.warn('  ', w.text);
  }
}
