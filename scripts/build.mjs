// Minimal build: stamps the version into src/area-events-card.js and writes dist/.
// No dependencies; the card is a single vanilla JS file.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

let commit = "nogit";
try {
  commit = execSync("git rev-parse --short HEAD", { cwd: root, stdio: ["ignore", "pipe", "ignore"] })
    .toString()
    .trim();
} catch {
  // Not a git checkout or no commits yet.
}

const version = `${pkg.version}+${commit}`;
const source = readFileSync(join(root, "src", "area-events-card.js"), "utf8");
if (!source.includes("__BUILD_VERSION__")) {
  throw new Error("src/area-events-card.js is missing the __BUILD_VERSION__ placeholder");
}

mkdirSync(join(root, "dist"), { recursive: true });
const out = join(root, "dist", "area-events-card.js");
writeFileSync(out, source.replaceAll("__BUILD_VERSION__", version));
console.log(`Built ${out} (version ${version})`);
