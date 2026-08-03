#!/usr/bin/env node
/*
 * build.js — compiles index.src.html (JSX) into index.html (plain JS, no in-browser Babel).
 *
 * Why: the old index.html shipped babel-standalone and transpiled ~60KB of JSX in the
 * browser on every page load. Precompiling removes that download and the per-load
 * transpile cost, so the page renders noticeably faster, especially on mobile and on
 * a cold Render start.
 *
 * Workflow:
 *   - Edit index.src.html (the JSX source). This is the file to change.
 *   - Run:  npm install @babel/core @babel/preset-react   (one time)
 *           node build.js
 *   - Deploy index.html (the generated file). Do not hand-edit index.html.
 */
const fs = require("fs");
const babel = require("@babel/core");

const SRC = "index.src.html";
const OUT = "index.html";

const src = fs.readFileSync(SRC, "utf8");

// Pull out the <script type="text/babel"> ... </script> block
const m = src.match(/<script type="text\/babel">([\s\S]*?)<\/script>/);
if (!m) { console.error("No <script type=\"text/babel\"> block found in " + SRC); process.exit(1); }

// runtime:"classic" is REQUIRED. Babel 8 defaults to the "automatic" runtime, which emits
// `import { jsx } from "react/jsx-runtime"` statements. Those cannot run inside a plain
// <script> tag, so the page loads to a blank screen. Classic emits React.createElement calls,
// which work against the React UMD build loaded from the CDN.
const compiled = babel.transformSync(m[1], {
  presets: [["@babel/preset-react", { runtime: "classic" }]],
}).code;

// Fail loudly rather than shipping a file that renders nothing.
if (/^\s*import\s/m.test(compiled)) {
  console.error("Compiled output contains ES import statements, which will not run in a plain <script> tag. Aborting.");
  process.exit(1);
}

let out = src
  // drop the babel-standalone CDN script
  .replace(/\s*<script src="https:\/\/cdnjs\.cloudflare\.com\/ajax\/libs\/babel-standalone\/[^"]+"><\/script>/, "")
  // swap the babel block for a plain script holding the compiled JS
  .replace(/<script type="text\/babel">[\s\S]*?<\/script>/,
    () => '<script>\n/* Precompiled from ' + SRC + ' via Babel. Edit ' + SRC + ' and re-run build.js, not this file. */\n' + compiled + '\n  </script>');

fs.writeFileSync(OUT, out);
console.log("Built " + OUT + " (" + out.length + " bytes) from " + SRC);
