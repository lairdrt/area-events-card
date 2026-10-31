// Minimal build: stamps the host card with its build identifier and copies the module folders it imports (presentation, filters) to
//   custom_components/area_events/frontend/   (what the integration serves; committed, so installs need no build)
//   dist/                                     (generated, not stored in Git)
// The identifier is "AREA-EVENTS <integration version>-<hash of the source>", shown in the card footer and
// logged to the browser console. The source keeps the __AREA_EVENTS_CARD_BUILD__ placeholder.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// The source hash must be platform-independent, but the checked-out, TRACKED HA
// assets must retain their existing line endings. Git text=auto checks these files
// out as CRLF on Windows and LF on Linux; overwriting CRLF with LF dirties Git.
const canonical = (text) => text.replace(/\r\n?/g, "\n");
const readCanonical = (path) => canonical(readFileSync(path, "utf8"));

function writeTracked(path, generatedLF) {
  if (existsSync(path)) {
    const existing = readFileSync(path, "utf8");
    if (canonical(existing) === generatedLF) return; // Crucial: do not touch clean tracked files.
    const eol = existing.includes("\r\n") ? "\r\n" : "\n";
    writeFileSync(path, generatedLF.replace(/\n/g, eol));
  } else {
    const eol = process.platform === "win32" ? "\r\n" : "\n";
    writeFileSync(path, generatedLF.replace(/\n/g, eol));
  }
}

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const sourceDir = join(root, "src", "frontend");
const source = readCanonical(join(sourceDir, "area-events-card.js"));
if (!source.includes("__AREA_EVENTS_CARD_BUILD__")) {
  throw new Error("src/frontend/area-events-card.js is missing the __AREA_EVENTS_CARD_BUILD__ placeholder");
}
const { version } = JSON.parse(readFileSync(join(root, "custom_components", "area_events", "manifest.json"), "utf8"));
// The folders of modules the card imports (renderers, filters); each is served by the integration beside the card.
const MODULE_FOLDERS = ["presentation", "filters"];
const modules = MODULE_FOLDERS.map((folder) => {
  const files = readdirSync(join(sourceDir, folder)).filter((name) => name.endsWith(".js")).sort();
  if (!files.length) throw new Error(`src/frontend/${folder} has no modules`);
  return { folder, files };
});
const moduleSource = modules.flatMap(({ folder, files }) =>
  files.map((name) => `${folder === "presentation" ? "" : `${folder}/`}${name}\n${readCanonical(join(sourceDir, folder, name))}`)).join("\n");
const identifier = `AREA-EVENTS ${version}-${createHash("sha1").update(source).update(moduleSource).digest("hex").slice(0, 8)}`;
const built = source.replaceAll("__AREA_EVENTS_CARD_BUILD__", identifier);

// The tracked integration bundle is committed for installation without a build.
// Do NOT rewrite it when only the working tree's line endings differ.
const tracked = join(root, "custom_components", "area_events", "frontend");
mkdirSync(tracked, { recursive: true });
writeTracked(join(tracked, "area-events-card.js"), built);
for (const { folder, files } of modules) {
  mkdirSync(join(tracked, folder), { recursive: true });
  for (const name of files) writeTracked(join(tracked, folder, name), readCanonical(join(sourceDir, folder, name)));
}

// dist is untracked and can always use canonical LF bytes.
const dist = join(root, "dist");
mkdirSync(dist, { recursive: true });
writeFileSync(join(dist, "area-events-card.js"), built);
for (const { folder, files } of modules) {
  mkdirSync(join(dist, folder), { recursive: true });
  for (const name of files) writeFileSync(join(dist, folder, name), readCanonical(join(sourceDir, folder, name)));
}
console.log(`Built ${join(tracked, "area-events-card.js")} (${identifier})`);
console.log(`Built ${join(dist, "area-events-card.js")} (${identifier})`);
