import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const original = (path) => readFileSync(join(root, path), "utf8").replace(/\r\n?/g, "\n");

// Exercise the actual build script against an isolated CRLF checkout (not a mocked
// hash function), and compare the exact bytes with the checked-in HA card bundle.
test("frontend build is deterministic for both LF and Windows CRLF inputs", () => {
  const temporary = mkdtempSync(join(tmpdir(), "area-events-eol-"));
  try {
    const presentation = readdirSync(join(root, "src/frontend/presentation"))
      .filter((name) => name.endsWith(".js"));
    const inputs = ["src/frontend/area-events-card.js",
      ...presentation.map((name) => "src/frontend/presentation/" + name)];
    mkdirSync(join(temporary, "scripts"), { recursive: true });
    mkdirSync(join(temporary, "custom_components/area_events"), { recursive: true });
    cpSync(join(root, "scripts/build.mjs"), join(temporary, "scripts/build.mjs"));
    writeFileSync(join(temporary, "custom_components/area_events/manifest.json"),
      original("custom_components/area_events/manifest.json"));
    const expected = original("custom_components/area_events/frontend/area-events-card.js");
    for (const lineEnding of ["\n", "\r\n"]) {
      for (const input of inputs) {
        const destination = join(temporary, input);
        mkdirSync(dirname(destination), { recursive: true });
        writeFileSync(destination, original(input).replace(/\n/g, lineEnding));
      }
      execFileSync(process.execPath, [join(temporary, "scripts/build.mjs")],
        { cwd: temporary, stdio: "pipe" });
      const actual = readFileSync(
        join(temporary, "custom_components/area_events/frontend/area-events-card.js"), "utf8");
      assert.equal(actual, expected, "build must match committed bundle with " +
        (lineEnding === "\n" ? "LF" : "CRLF") + " inputs");
      for (const name of presentation) {
        const module = readFileSync(
          join(temporary, "custom_components/area_events/frontend/presentation", name), "utf8");
        assert.equal(module, original("src/frontend/presentation/" + name),
          "presentation module " + name + " must not change across line endings");
      }
    }
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
