/**
 * Playwright global teardown — kills the hub subprocess started in global-setup.ts.
 */

import { ChildProcess } from "child_process";

export default async function globalTeardown() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const hubProc: ChildProcess | undefined = (globalThis as any).__HUB_PROC__;
  if (hubProc && !hubProc.killed) {
    hubProc.kill("SIGTERM");
    // Give it up to 3s to exit cleanly before forcing.
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        hubProc.kill("SIGKILL");
        resolve();
      }, 3_000);
      hubProc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
    console.log("[e2e] Hub process terminated.");
  }
}
