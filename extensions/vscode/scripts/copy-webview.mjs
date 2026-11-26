// Copies the T16/T17 web bundle (src/wildcat_code_agent/web) into webview/ so the extension can load it from its own
// folder. The copy is generated (git-ignored): the bundle has one source, the Python package.
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "..", "..", "src", "wildcat_code_agent", "web");
const target = join(here, "..", "webview");

if (!existsSync(join(source, "index.html"))) {
  console.error(`copy-webview: the bundle is not at ${source}`);
  process.exit(1);
}
rmSync(target, { recursive: true, force: true });
cpSync(source, target, { recursive: true });
console.log(`copy-webview: ${source} -> ${target}`);
