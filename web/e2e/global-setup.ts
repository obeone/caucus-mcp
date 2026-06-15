/**
 * Playwright global setup — boots the real Python Caucus hub as a subprocess.
 *
 * Steps:
 * 1. Find a free TCP port.
 * 2. Build the frontend bundle (hub serves it from src/caucus/ui/).
 * 3. Spawn `python -m caucus.hub --host 127.0.0.1 --port <port>` using the
 *    repo .venv (../.venv/bin/python relative to web/).
 * 4. Poll GET http://127.0.0.1:<port>/ until HTTP 200 (max 15s).
 * 5. Write the base URL into process.env.E2E_BASE_URL for tests.
 * 6. Store the child process handle in globalThis.__HUB_PROC__ so
 *    global-teardown.ts can kill it.
 */

import { spawn, ChildProcess } from "child_process";
import * as http from "http";
import * as net from "net";
import * as path from "path";
import * as fs from "fs";
import { fileURLToPath } from "url";

// ESM-compatible __dirname equivalent.
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/** Find an available TCP port by binding to :0 and releasing. */
async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (!addr || typeof addr === "string") {
        srv.close(() => reject(new Error("Could not get port")));
        return;
      }
      const port = addr.port;
      srv.close(() => resolve(port));
    });
  });
}

/** Poll a URL until it returns HTTP 200 (or timeout). */
async function waitForHttp(url: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await new Promise<boolean>((resolve) => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      });
      req.on("error", () => resolve(false));
      req.setTimeout(1_000, () => {
        req.destroy();
        resolve(false);
      });
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Hub at ${url} did not respond within ${timeoutMs}ms`);
}

export default async function globalSetup() {
  const webDir = path.resolve(__dirname, "..");
  const repoRoot = path.resolve(webDir, "..");

  // Resolve Python interpreter — prefer the repo venv.
  const venvPython = path.join(repoRoot, ".venv", "bin", "python");
  const python = fs.existsSync(venvPython) ? venvPython : "python3";

  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;

  process.env["E2E_BASE_URL"] = baseUrl;
  process.env["E2E_HUB_PORT"] = String(port);

  const hubProc: ChildProcess = spawn(
    python,
    ["-m", "caucus.hub", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: repoRoot,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env },
    }
  );

  hubProc.stdout?.on("data", (chunk: Buffer) => {
    process.stdout.write(`[hub] ${chunk}`);
  });
  hubProc.stderr?.on("data", (chunk: Buffer) => {
    process.stderr.write(`[hub] ${chunk}`);
  });

  // Expose to teardown via globalThis (Playwright preserves this across files).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).__HUB_PROC__ = hubProc;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).__HUB_PORT__ = port;

  await waitForHttp(`${baseUrl}/`, 20_000);
  console.log(`[e2e] Hub ready at ${baseUrl}`);
}
