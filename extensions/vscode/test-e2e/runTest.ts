// Starts a real VS Code with the extension in development mode and runs test-e2e/suite in its extension host.
// Needs a display. VSCODE_PATH (the Code executable) picks the install; without it the test-electron default
// download is used. Run with `npm run test:e2e`. Not part of `npm test`.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "..", "..");
  const extensionTestsPath = path.resolve(__dirname, "suite", "index");
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "local-agent-e2e-"));
  fs.writeFileSync(path.join(workspace, "hello.txt"), "Hello world\n", "utf8");
  await runTests({
    extensionDevelopmentPath,
    extensionTestsPath,
    vscodeExecutablePath: process.env.VSCODE_PATH || undefined,
    launchArgs: [workspace, "--disable-extensions", "--skip-welcome", "--skip-release-notes"],
  });
}

main().catch((error) => {
  console.error("E2E failed:", error);
  process.exit(1);
});
