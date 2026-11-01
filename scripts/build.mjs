// Minimal build: stamps the host card with its build identifier and copies the module folders it imports (presentation, filters), every relative
// import fingerprinted with the imported module's content hash (see versionImports below), to
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

// Every relative import names the module it loads with that module's content fingerprint ("./community.js?v=<sha-256 of its built text>"). Home Assistant
// gives only the card a new address per deployment (?v=<file time>); the browser may keep using a cached module for an address it has seen, even on a hard
// reload. A changed module therefore gets a new address, and so does every module above it up to the card (each fingerprint covers the
// fingerprints it imports); an unchanged module keeps its address and its cached copy. The server ignores the query.
const RELATIVE_IMPORT = /(\b(?:import|export)\b[^;]*?\bfrom\s*|\bimport\s*)(["'])(\.\.?\/[^"'?]+\.js)\2/g;
const fingerprints = new Map();
function versionImports(text, path, chain) {
  if (/\bimport\s*\(\s*["'`]\.\.?\//.test(text)) throw new Error(`${path} imports a module dynamically; its address would not be fingerprinted`);
  return text.replace(RELATIVE_IMPORT, (_, head, quote, specifier) => `${head}${quote}${specifier}?v=${fingerprint(join(dirname(path), specifier), [...chain, path]).version}${quote}`);
}
function fingerprint(path, chain = []) {
  if (chain.includes(path)) throw new Error(`import cycle: ${[...chain, path].join(" -> ")}`);
  if (!fingerprints.has(path)) {
    if (!path.startsWith(sourceDir) || !existsSync(path)) throw new Error(`${chain.at(-1)} imports a missing module: ${path}`);
    const text = versionImports(readCanonical(path), path, chain);
    fingerprints.set(path, { text, version: createHash("sha256").update(text).digest("hex").slice(0, 12) });
  }
  return fingerprints.get(path);
}
const servedModule = (folder, name) => fingerprint(join(sourceDir, folder, name)).text;
// The identifier covers the modules as served (their fingerprinted imports included), so a different module graph is a different build.
const moduleSource = modules.flatMap(({ folder, files }) =>
  files.map((name) => `${folder === "presentation" ? "" : `${folder}/`}${name}\n${servedModule(folder, name)}`)).join("\n");
const identifier = `AREA-EVENTS ${version}-${createHash("sha1").update(source).update(moduleSource).digest("hex").slice(0, 8)}`;
const built = versionImports(source.replaceAll("__AREA_EVENTS_CARD_BUILD__", identifier), join(sourceDir, "area-events-card.js"), []);

// The tracked integration bundle is committed for installation without a build.
// Do NOT rewrite it when only the working tree's line endings differ.
const tracked = join(root, "custom_components", "area_events", "frontend");
mkdirSync(tracked, { recursive: true });
writeTracked(join(tracked, "area-events-card.js"), built);
for (const { folder, files } of modules) {
  mkdirSync(join(tracked, folder), { recursive: true });
  for (const name of files) writeTracked(join(tracked, folder, name), servedModule(folder, name));
}

// dist is untracked and can always use canonical LF bytes.
const dist = join(root, "dist");
mkdirSync(dist, { recursive: true });
writeFileSync(join(dist, "area-events-card.js"), built);
for (const { folder, files } of modules) {
  mkdirSync(join(dist, folder), { recursive: true });
  for (const name of files) writeFileSync(join(dist, folder, name), servedModule(folder, name));
}
console.log(`Built ${join(tracked, "area-events-card.js")} (${identifier})`);
console.log(`Built ${join(dist, "area-events-card.js")} (${identifier})`);
