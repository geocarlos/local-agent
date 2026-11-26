// Checks what `vsce package` needs and what the .vsix carries (T22). It uses the packaging module's listFiles, which
// applies .vscodeignore the way the .vsix does, so no .vsix is built here. Needs `npm run compile` (npm test does it).
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import * as assert from "node:assert/strict";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const manifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

// The lowest version a range allows: "^1.146.0" and "~1.146.0" both give 1.146.0.
function baseVersion(range) {
  const match = /^[\^~]?(\d+)\.(\d+)\.(\d+)$/.exec(range);
  assert.ok(match, `a plain version range is expected, got ${range}`);
  return match.slice(1).map(Number);
}

function compareVersions(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}

test("package.json has the fields vsce needs", () => {
  assert.match(manifest.publisher, /^[a-z0-9][a-z0-9-]*$/i, "publisher");
  assert.match(manifest.name, /^[a-z0-9][a-z0-9-]*$/, "name");
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/, "version");
  assert.ok(manifest.displayName, "displayName");
  assert.ok(manifest.description, "description");
  assert.ok(manifest.repository?.url, "repository.url (vsce would need --allow-missing-repository without it)");
  assert.ok(typeof manifest.license === "string" && manifest.license.length > 0, "license");
  assert.ok(existsSync(join(root, "README.md")), "README.md in the extension folder");
});

test("main points into out/ and the compiled file exists", () => {
  assert.match(manifest.main, /^\.\/out\/.+\.js$/, `main is ${manifest.main}`);
  assert.ok(existsSync(join(root, manifest.main)), `${manifest.main} exists (run npm run compile)`);
});

test("the icon is a PNG of at least 128x128", () => {
  assert.equal(manifest.icon, "media/icon.png");
  const bytes = readFileSync(join(root, manifest.icon));
  assert.deepEqual([...bytes.subarray(1, 4)], [0x50, 0x4e, 0x47], "PNG signature");
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  assert.ok(width >= 128 && height >= 128, `icon is ${width}x${height}`);
});

test("engines.vscode is not below @types/vscode, so the API used is available", () => {
  const engines = baseVersion(manifest.engines.vscode);
  const types = baseVersion(manifest.devDependencies["@types/vscode"]);
  assert.ok(compareVersions(types, engines) <= 0, `@types/vscode ${types} is newer than engines ${engines}`);
});

test("the .vsix carries what runs and leaves out the sources and tests", () => {
  const { listFiles } = require("@vscode/vsce");
  return listFiles({ cwd: root, packagedDependencies: [] }).then((files) => {
    const shipped = new Set(files.map((file) => file.split("\\").join("/")));
    for (const required of [
      "package.json",
      "README.md",
      "media/icon.png",
      manifest.main.replace(/^\.\//, ""),
      "webview/index.html",
      "webview/js/main.js",
    ]) {
      assert.ok(shipped.has(required), `${required} is in the .vsix`);
    }
    const forbidden = [
      (file) => file.startsWith("src/"),
      (file) => file.startsWith("test/") || file.startsWith("test-e2e/"),
      (file) => file.startsWith("out/test/") || file.startsWith("out/test-e2e/"),
      (file) => file.startsWith("scripts/") || file.startsWith(".vscode-test/") || file.startsWith("node_modules/"),
      (file) => file.endsWith(".map") || file.endsWith(".ts") || file === "tsconfig.json",
      (file) => file === "package-lock.json" || file === ".vscodeignore" || file.endsWith(".vsix"),
    ];
    const leaked = [...shipped].filter((file) => forbidden.some((test) => test(file)));
    assert.deepEqual(leaked, [], "files that must not be in the .vsix");
  });
});
