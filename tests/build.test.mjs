import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const original = (path) => readFileSync(join(root, path), "utf8").replace(/\r\n?/g, "\n");
const normalize = (text) => text.replace(/\r\n?/g, "\n");

// Run the ACTUAL build against simulated Windows and Linux git checkouts.
// Both source files AND previously tracked generated files are CRLF on Windows.
test("frontend build leaves committed HA assets untouched under LF and CRLF checkouts", () => {
  const temporary = mkdtempSync(join(tmpdir(), "area-events-eol-"));
  try {
    const presentation = readdirSync(join(root, "src/frontend/presentation"))
      .filter((name) => name.endsWith(".js"));
    const filters = readdirSync(join(root, "src/frontend/filters"))
      .filter((name) => name.endsWith(".js"));
    assert.ok(filters.includes("date-windows.js"), "the date-window module is part of the build");
    const sources = ["src/frontend/area-events-card.js",
      ...presentation.map((name) => "src/frontend/presentation/" + name),
      ...filters.map((name) => "src/frontend/filters/" + name)];
    const tracked = ["custom_components/area_events/frontend/area-events-card.js",
      ...presentation.map((name) => "custom_components/area_events/frontend/presentation/" + name),
      ...filters.map((name) => "custom_components/area_events/frontend/filters/" + name)];
    mkdirSync(join(temporary, "scripts"), { recursive: true });
    mkdirSync(join(temporary, "custom_components/area_events"), { recursive: true });
    cpSync(join(root, "scripts/build.mjs"), join(temporary, "scripts/build.mjs"));
    writeFileSync(join(temporary, "custom_components/area_events/manifest.json"),
      original("custom_components/area_events/manifest.json"));
    const expectedBuilt = original(tracked[0]);

    for (const lineEnding of ["\n", "\r\n"]) {
      const endingName = lineEnding === "\n" ? "LF" : "CRLF";
      for (const relative of [...sources, ...tracked]) {
        const destination = join(temporary, relative);
        mkdirSync(dirname(destination), { recursive: true });
        writeFileSync(destination, original(relative).replace(/\n/g, lineEnding));
      }
      const before = new Map(tracked.map((relative) =>
        [relative, {
          bytes: readFileSync(join(temporary, relative), "utf8"),
          modified: statSync(join(temporary, relative)).mtimeMs,
        }]));

      execFileSync(process.execPath, [join(temporary, "scripts/build.mjs")],
        { cwd: temporary, stdio: "pipe" });

      for (const relative of tracked) {
        const actual = readFileSync(join(temporary, relative), "utf8");
        assert.equal(actual, before.get(relative).bytes,
          endingName + ": tracked file rewritten: " + relative);
        assert.equal(statSync(join(temporary, relative)).mtimeMs, before.get(relative).modified,
          endingName + ": tracked file touched: " + relative);
      }
      assert.equal(readFileSync(join(temporary, "dist/area-events-card.js"), "utf8"), expectedBuilt,
        endingName + ": dist bundle must match committed source");
      for (const name of presentation) {
        assert.equal(readFileSync(join(temporary, "dist/presentation", name), "utf8"),
          original("custom_components/area_events/frontend/presentation/" + name));
      }
      for (const name of filters) {
        assert.equal(readFileSync(join(temporary, "dist/filters", name), "utf8"),
          original("custom_components/area_events/frontend/filters/" + name));
      }

      // A legitimately changed source must regenerate the tracked bundle while
      // preserving the target file's checkout line ending and canonical build hash.
      const srcPath = join(temporary, sources[0]);
      const changed = readFileSync(srcPath, "utf8").replace("Craft Fairs", "Craft Marketplace Fairs");
      assert.notEqual(changed, readFileSync(srcPath, "utf8"), "source fixture did not change");
      writeFileSync(srcPath, changed);
      execFileSync(process.execPath, [join(temporary, "scripts/build.mjs")],
        { cwd: temporary, stdio: "pipe" });
      const updated = readFileSync(join(temporary, tracked[0]), "utf8");
      assert.notEqual(normalize(updated), expectedBuilt, "updated source must update bundle");
      assert.equal(normalize(updated),
        readFileSync(join(temporary, "dist/area-events-card.js"), "utf8"),
        endingName + ": tracked and dist bundle mismatch after change");
      if (lineEnding === "\r\n") {
        assert.ok(updated.includes("\r\n"), "Windows output lost CRLF checkout line endings");
        assert.ok(!/(^|[^\r])\n/.test(updated), "Windows output introduced LF-only lines");
      }
    }
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

// Every relative import of the built card and its modules carries the imported module's content fingerprint, so a browser that cached one version
// of a module cannot reuse it for another; an unchanged module keeps its address.
const IMPORT = /\bfrom\s*["'](\.\.?\/[^"'?]+\.js)(?:\?v=([0-9a-f]+))?["']/g;
function graph(frontend) {
  const found = new Map();
  const visit = (relative) => {
    if (found.has(relative)) return;
    const text = normalize(readFileSync(join(frontend, relative), "utf8"));
    const imports = [...text.matchAll(IMPORT)].map(([, specifier, version]) => ({ target: join(dirname(relative), specifier).replace(/\\/g, "/"), version }));
    found.set(relative, { text, imports });
    for (const { target } of imports) visit(target);
  };
  visit("area-events-card.js");
  return found;
}
const fingerprintOf = (text) => createHash("sha256").update(text).digest("hex").slice(0, 12);

test("the built module graph is fingerprinted end to end and a changed renderer changes only its own chain", () => {
  const committed = graph(join(root, "custom_components/area_events/frontend"));
  assert.deepEqual([...committed.keys()].sort(), ["area-events-card.js", "filters/date-windows.js", "presentation/community.js", "presentation/contract.js",
    "presentation/generic.js", "presentation/manifest.js", "presentation/nasa-mission.js", "presentation/registry.js"]);
  for (const [name, { imports }] of committed) {
    for (const { target, version } of imports) {
      assert.equal(version, fingerprintOf(committed.get(target).text), `${name} imports ${target} with the fingerprint of its served text`);
    }
  }
  const sourceImports = [...original("src/frontend/presentation/manifest.js").matchAll(IMPORT)];
  assert.ok(sourceImports.length && sourceImports.every(([, , version]) => version === undefined), "the sources keep plain imports");

  const temporary = mkdtempSync(join(tmpdir(), "area-events-fingerprint-"));
  try {
    cpSync(join(root, "src/frontend"), join(temporary, "src/frontend"), { recursive: true });
    mkdirSync(join(temporary, "scripts"), { recursive: true });
    cpSync(join(root, "scripts/build.mjs"), join(temporary, "scripts/build.mjs"));
    mkdirSync(join(temporary, "custom_components/area_events"), { recursive: true });
    writeFileSync(join(temporary, "custom_components/area_events/manifest.json"), original("custom_components/area_events/manifest.json"));
    const rebuild = () => { execFileSync(process.execPath, [join(temporary, "scripts/build.mjs")], { cwd: temporary, stdio: "pipe" });
      return graph(join(temporary, "custom_components/area_events/frontend")); };
    const before = rebuild();
    assert.deepEqual([...before].map(([name, { text }]) => [name, text]), [...committed].map(([name, { text }]) => [name, text]), "the build is deterministic");
    const community = join(temporary, "src/frontend/presentation/community.js");
    writeFileSync(community, readFileSync(community, "utf8").replace("`Source: ${event.sourceName}`", "`Source: ${event.sourceName} (changed)`"));
    const after = rebuild();
    const changed = [...after.keys()].filter((name) => after.get(name).text !== before.get(name).text).sort();
    assert.deepEqual(changed, ["area-events-card.js", "presentation/community.js", "presentation/manifest.js"], "the renderer and every module above it, up to the card");
    assert.notEqual(after.get("area-events-card.js").text.match(/AREA-EVENTS \S+-[0-9a-f]{8}/)[0], before.get("area-events-card.js").text.match(/AREA-EVENTS \S+-[0-9a-f]{8}/)[0],
      "a different module graph is a different build");
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
