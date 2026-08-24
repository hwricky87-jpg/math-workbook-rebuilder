#!/usr/bin/env node
// Rasterize arbitrary SVG through a local or geometry-dsl resvg backend.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const packageCandidates = [
  path.resolve(here, "..", "package.json"),
  path.resolve(here, "..", "..", "geometry-dsl", "package.json"),
];

let Resvg;
let loadError;
for (const packageJson of packageCandidates) {
  if (!fs.existsSync(packageJson)) continue;
  try {
    ({ Resvg } = createRequire(packageJson)("@resvg/resvg-js"));
    break;
  } catch (error) {
    loadError = error;
  }
}
if (!Resvg) {
  console.error("@resvg/resvg-js is required. Run install.ps1 -InstallDependencies or npm ci --omit=dev --ignore-scripts in the skill folder.");
  if (loadError) console.error(loadError.message);
  process.exit(2);
}

const [input, output, scaleText = "2"] = process.argv.slice(2);
if (!input || !output) {
  console.error("usage: node render_svg.mjs input.svg output.png [scale]");
  process.exit(2);
}
const scale = Number(scaleText);
if (!Number.isFinite(scale) || scale <= 0) {
  console.error("scale must be a positive number");
  process.exit(2);
}

const renderer = new Resvg(fs.readFileSync(input, "utf8"), {
  background: "white",
  fitTo: { mode: "zoom", value: scale },
  font: {
    fontFiles: ["C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgunbd.ttf"].filter(fs.existsSync),
    loadSystemFonts: true,
  },
});
fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true });
fs.writeFileSync(output, renderer.render().asPng());
console.log(output);
