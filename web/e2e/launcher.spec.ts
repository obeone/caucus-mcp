/**
 * E2E tests for launching a Codex agent from the dashboard's Agent Launcher.
 *
 * The launcher only exists on a hub started with an operator token, the
 * launcher flag and an agent cwd, which the shared hub from global-setup.ts
 * deliberately is not. This spec therefore boots its OWN hub (own port, own
 * temp agent cwd) in beforeAll and kills it in afterAll.
 *
 * SAFETY: the developer machine may have the real `codex` CLI logged in to a
 * paid subscription, and no test may ever start a real Codex session. The
 * launcher hub gets `CODEX_PATH` set to an absolute path to a stub script that
 * records its invocation and exits non-zero. `codex_agent` resolves the binary with
 * `shutil.which(CODEX_PATH)`, and `which` on a path containing a slash never
 * consults PATH, so the real binary cannot be reached. As belt and braces,
 * every PATH entry holding an executable named `codex` is also removed from
 * the hub environment, and `CODEX_HOME` points at an empty temp directory so
 * no credentials exist even if the stub were bypassed.
 */

import { test, expect, Page, Dialog } from "@playwright/test";
import { spawn, ChildProcess } from "child_process";
import * as fs from "fs";
import * as net from "net";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..");

const OPERATOR_TOKEN = "e2e-operator-token-launcher";
const AGENT_NAME = "codex-e2e";
const MARKER = "FAKE_CODEX_STUB_MARKER_7f3a9c";

let hubProc: ChildProcess | undefined;
let tmpRoot = "";
let baseUrl = "";
let hubLog = "";
let stubLog = "";

/** Find a free loopback TCP port by binding to :0 and releasing it. */
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

/** Poll `url` until it answers HTTP 200, or throw after `timeoutMs`. */
async function waitForHub(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.status === 200) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Launcher hub at ${url} did not respond within ${timeoutMs}ms`);
}

/** PATH with every directory that holds an executable `codex` removed. */
function pathWithoutCodex(): string {
  const dirs = (process.env["PATH"] ?? "").split(path.delimiter);
  return dirs
    .filter((d) => {
      if (!d) return false;
      try {
        fs.accessSync(path.join(d, "codex"), fs.constants.X_OK);
        return false;
      } catch {
        return true;
      }
    })
    .join(path.delimiter);
}

/** GET /agents with the operator bearer token. */
async function listAgents(): Promise<Array<Record<string, unknown>>> {
  const res = await fetch(`${baseUrl}/agents`, {
    headers: { Authorization: `Bearer ${OPERATOR_TOKEN}` },
  });
  expect(res.status).toBe(200);
  const body = (await res.json()) as { agents: Array<Record<string, unknown>> };
  return body.agents;
}

/**
 * Answer successive `window.prompt` dialogs with the given tokens, in order.
 * Returns the list of prompts seen, so a test can count them.
 */
function answerPrompts(page: Page, answers: string[]): Dialog[] {
  const seen: Dialog[] = [];
  page.on("dialog", (dialog) => {
    seen.push(dialog);
    const answer = answers[seen.length - 1];
    if (dialog.type() === "prompt" && answer !== undefined) {
      void dialog.accept(answer);
    } else {
      void dialog.dismiss();
    }
  });
  return seen;
}

test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  // realpath: the hub refuses an agent cwd that resolves elsewhere (macOS /var).
  tmpRoot = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "caucus-e2e-launcher-"))
  );
  const binDir = path.join(tmpRoot, "bin");
  const codexHome = path.join(tmpRoot, "codex-home");
  const agentCwd = path.join(tmpRoot, "agent-cwd");
  for (const d of [binDir, codexHome, agentCwd]) fs.mkdirSync(d);

  // codex_agent captures the app-server's stderr instead of forwarding it, so
  // the stub also records each invocation in a file the test can read back.
  stubLog = path.join(tmpRoot, "stub-invocations.log");
  const fakeCodex = path.join(binDir, "codex");
  fs.writeFileSync(
    fakeCodex,
    `#!/bin/sh\necho "${MARKER} $*" >> "${stubLog}"\n` +
      `echo "${MARKER} (fake codex, args: $*)" >&2\nexit 7\n`,
    { mode: 0o755 }
  );

  const venvPython = path.join(repoRoot, ".venv", "bin", "python");
  const python = fs.existsSync(venvPython) ? venvPython : "python3";
  const port = await freePort();
  baseUrl = `http://127.0.0.1:${port}`;

  // Drop inherited credentials and CAUCUS_* settings so the hub is hermetic.
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (/^(CAUCUS_|CODEX_|OPENAI_|ANTHROPIC_)/.test(k)) continue;
    env[k] = v;
  }
  env["PATH"] = pathWithoutCodex();
  env["CODEX_PATH"] = fakeCodex;
  env["CODEX_HOME"] = codexHome;

  hubProc = spawn(
    python,
    [
      "-m", "caucus.hub",
      "--host", "127.0.0.1",
      "--port", String(port),
      "--no-browser",
      "--operator-token", OPERATOR_TOKEN,
      "--enable-agent-launcher",
      "--agent-cwd", agentCwd,
    ],
    { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"], env }
  );
  const collect = (chunk: Buffer) => {
    hubLog += chunk.toString();
  };
  hubProc.stdout?.on("data", collect);
  hubProc.stderr?.on("data", collect);

  try {
    await waitForHub(`${baseUrl}/`, 20_000);
  } catch (err) {
    hubProc.kill("SIGKILL");
    throw new Error(`${String(err)}\n--- hub output ---\n${hubLog}`);
  }
});

test.afterAll(async () => {
  const proc = hubProc;
  if (proc && proc.exitCode === null) {
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        proc.kill("SIGKILL");
        resolve();
      }, 5_000);
      proc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
      proc.kill("SIGTERM");
    });
  }
  if (tmpRoot) fs.rmSync(tmpRoot, { recursive: true, force: true });
});

test.describe("Agent Launcher: Codex runtime", () => {
  test("rejects a wrong token, then connects after retrying with the right one", async ({
    page,
  }) => {
    const prompts = answerPrompts(page, ["definitely-wrong-token", OPERATOR_TOKEN]);
    await page.goto(baseUrl);

    // The wrong token is refused (auth_error) and the socket reconnects, which
    // presents a fresh prompt without any page reload.
    await expect(page.getByLabel(/Connection: connected/i)).toBeVisible({
      timeout: 20_000,
    });
    expect(prompts.length).toBe(2);
    expect(prompts[0].message()).toMatch(/token/i);
    await expect(page.getByRole("region", { name: "Agent launcher" })).toBeVisible();
  });

  test("Codex runtime restricts permission modes, spawns, and reports the stub exit", async ({
    page,
  }) => {
    answerPrompts(page, [OPERATOR_TOKEN]);
    await page.goto(baseUrl);
    await expect(page.getByLabel(/Connection: connected/i)).toBeVisible({
      timeout: 15_000,
    });
    const launcher = page.getByRole("region", { name: "Agent launcher" });
    const modes = launcher.getByLabel("Agent permission mode");

    await test.step("Codex restricts permission modes", async () => {
      // Claude (default runtime) offers all six modes.
      await expect(modes.locator("option")).toHaveCount(6);
      await launcher
        .getByLabel("Agent runtime")
        .selectOption({ label: "Codex — ChatGPT subscription" });
      await expect(modes.locator("option")).toHaveText([
        "auto",
        "default",
        "acceptEdits",
        "plan",
      ]);
    });

    await test.step("spawning a talker shows the toast and a roster row", async () => {
      await launcher.getByLabel("Agent name").fill(AGENT_NAME);
      await launcher.getByLabel("Agent mission").fill("Say hello, then stop.");
      await launcher.getByRole("button", { name: "Spawn agent" }).click();
      await expect(page.getByText(`Spawned ${AGENT_NAME}`, { exact: true })).toBeVisible();
      const row = launcher.getByRole("list", { name: "Supervised agents" });
      await expect(row).toContainText(AGENT_NAME);
      await expect(row).toContainText("codex");
    });

    await test.step("hub record is a Codex launch run against the stub", async () => {
      let found: Record<string, unknown> | undefined;
      await expect
        .poll(
          async () => {
            found = (await listAgents()).find((a) => a["name"] === AGENT_NAME);
            if (!found) return "";
            const out = [
              ...((found["stdout"] as string[] | undefined) ?? []),
              ...((found["stderr"] as string[] | undefined) ?? []),
            ].join("\n");
            return out;
          },
          { timeout: 20_000, intervals: [250, 500, 1000] }
        )
        // The Codex module parsed its argv, joined the room, started its
        // app-server and saw it die: the whole launch chain ran.
        .toContain("Codex app-server disconnected");
      expect(found?.["runtime"]).toBe("codex");
      // And the app-server it started was the stub, never a real `codex`.
      const invocations = fs.readFileSync(stubLog, "utf8");
      expect(invocations).toContain(`${MARKER} app-server`);
    });

    await test.step("the agent ends exited and the UI shows it", async () => {
      await expect
        .poll(
          async () =>
            (await listAgents()).find((a) => a["name"] === AGENT_NAME)?.["state"],
          { timeout: 20_000, intervals: [250, 500, 1000] }
        )
        .toBe("exited");
      const list = launcher.getByRole("list", { name: "Supervised agents" });
      await expect(list).toContainText(/exited/, { timeout: 10_000 });
    });
  });
});
