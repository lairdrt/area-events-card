// Minimal build: stamps src/area-events-card.js with its build identifier and writes it to
//   custom_components/area_events/frontend/   (what the integration serves; committed, so installs need no build)
//   dist/                                     (generated, not stored in Git)
// The identifier is "AREA-EVENTS <integration version>-<hash of the source>", shown in the card footer and
// logged to the browser console. The source keeps the __AREA_EVENTS_CARD_BUILD__ placeholder.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = readFileSync(join(root, "src", "area-events-card.js"), "utf8");
if (!source.includes("__AREA_EVENTS_CARD_BUILD__")) {
  throw new Error("src/area-events-card.js is missing the __AREA_EVENTS_CARD_BUILD__ placeholder");
}
const { version } = JSON.parse(readFileSync(join(root, "custom_components", "area_events", "manifest.json"), "utf8"));
const identifier = `AREA-EVENTS ${version}-${createHash("sha1").update(source).digest("hex").slice(0, 8)}`;
const built = source.replaceAll("__AREA_EVENTS_CARD_BUILD__", identifier);

for (const folder of [join(root, "custom_components", "area_events", "frontend"), join(root, "dist")]) {
  mkdirSync(folder, { recursive: true });
  writeFileSync(join(folder, "area-events-card.js"), built);
  console.log(`Built ${join(folder, "area-events-card.js")} (${identifier})`);
}
