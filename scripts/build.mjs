// Minimal build: stamps the host card with its build identifier and copies registered presentation modules to
//   custom_components/area_events/frontend/   (what the integration serves; committed, so installs need no build)
//   dist/                                     (generated, not stored in Git)
// The identifier is "AREA-EVENTS <integration version>-<hash of the source>", shown in the card footer and
// logged to the browser console. The source keeps the __AREA_EVENTS_CARD_BUILD__ placeholder.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const sourceDir = join(root, "src", "frontend");
const source = readFileSync(join(sourceDir, "area-events-card.js"), "utf8");
if (!source.includes("__AREA_EVENTS_CARD_BUILD__")) {
  throw new Error("src/frontend/area-events-card.js is missing the __AREA_EVENTS_CARD_BUILD__ placeholder");
}
const { version } = JSON.parse(readFileSync(join(root, "custom_components", "area_events", "manifest.json"), "utf8"));
const presentationDir = join(sourceDir, "presentation");
const presentationFiles = readdirSync(presentationDir).filter((name) => name.endsWith(".js")).sort();
if (!presentationFiles.length) throw new Error("src/frontend/presentation has no renderer modules");
const moduleSource = presentationFiles.map((name) => `${name}\n${readFileSync(join(presentationDir, name), "utf8")}`).join("\n");
const identifier = `AREA-EVENTS ${version}-${createHash("sha1").update(source).update(moduleSource).digest("hex").slice(0, 8)}`;
const built = source.replaceAll("__AREA_EVENTS_CARD_BUILD__", identifier);

for (const folder of [join(root, "custom_components", "area_events", "frontend"), join(root, "dist")]) {
  mkdirSync(folder, { recursive: true });
  writeFileSync(join(folder, "area-events-card.js"), built);
  const modules = join(folder, "presentation");
  mkdirSync(modules, { recursive: true });
  for (const name of presentationFiles) writeFileSync(join(modules, name), readFileSync(join(presentationDir, name)));
  console.log(`Built ${join(folder, "area-events-card.js")} (${identifier})`);
}
