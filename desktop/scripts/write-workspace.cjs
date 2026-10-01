#!/usr/bin/env node
/**
 * Writes desktop/resources/workspace.json = {"workspace": "<absolute repo root>"}.
 * Bundled into the packaged app (extraResources) so that Watermark Lab.app
 * finds the workspace without asking.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const desktopDir = path.resolve(__dirname, "..");
const workspace = path.resolve(desktopDir, "..");
const outFile = path.join(desktopDir, "resources", "workspace.json");

if (!fs.existsSync(path.join(workspace, "workspace.yaml"))) {
  console.warn(`[write-workspace] Attention : ${workspace} ne contient pas de workspace.yaml`);
}

fs.mkdirSync(path.dirname(outFile), { recursive: true });
const content = JSON.stringify({ workspace }, null, 2) + "\n";
let previous = null;
try {
  previous = fs.readFileSync(outFile, "utf8");
} catch {
  /* first run */
}
if (previous !== content) fs.writeFileSync(outFile, content);
console.log(`[write-workspace] ${path.relative(process.cwd(), outFile) || outFile} → ${workspace}`);
