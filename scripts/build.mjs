// Minimal build: copies src/area-events-card.js to dist/ unchanged.
// The "__AREA_EVENTS_CARD_BUILD__" placeholder is kept verbatim; deploy.ps1
// injects the real build identifier into the copy it writes to Home Assistant.
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(root, "src", "area-events-card.js");
if (!readFileSync(src, "utf8").includes("__AREA_EVENTS_CARD_BUILD__")) {
  throw new Error("src/area-events-card.js is missing the __AREA_EVENTS_CARD_BUILD__ placeholder");
}

mkdirSync(join(root, "dist"), { recursive: true });
const out = join(root, "dist", "area-events-card.js");
copyFileSync(src, out);
console.log(`Built ${out}`);
