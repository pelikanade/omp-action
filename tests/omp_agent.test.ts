/**
 * Tests for the omp-agent command and for the action's input mapping.
 *
 * A fake `omp` records every invocation, NUL separated, so the tests can check
 * both the command line and the order of the `omp skill install` calls before
 * it. The command itself runs as a subprocess, which is how the action runs it.
 */

import { describe, expect, test } from "bun:test";
import { chmodSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildCommand, type Options } from "../omp_agent.ts";

const SHEBANG = `#!${Bun.which("bash") ?? "/usr/bin/env bash"}`;
const CLI = join(import.meta.dir, "..", "omp_agent.ts");
const PROMPT = "# Repair the failed update\n\n- fix `strata`\n";

interface Sandbox {
  dir: string;
  argv: string;
  env: Record<string, string>;
}

function sandbox(): Sandbox {
  const dir = mkdtempSync(join(tmpdir(), "omp-agent-test-"));
  const argv = join(dir, "argv.bin");
  return {
    dir,
    argv,
    env: {
      ...process.env,
      FAKE_ARGV: argv,
      GITHUB_OUTPUT: join(dir, "github-output"),
      OMP_MODEL: "",
      OMP_AGENT_BIN: "",
    },
  };
}

function fakeOmp(box: Sandbox, body = "exit 0\n"): string {
  const path = join(box.dir, "fake-omp");
  writeFileSync(
    path,
    `${SHEBANG}\nprintf '%s\\0' "$@" >> "$FAKE_ARGV"\nprintf '\\0' >> "$FAKE_ARGV"\n${body}`,
  );
  chmodSync(path, 0o755);
  return path;
}

function invocations(box: Sandbox): string[][] {
  if (!existsSync(box.argv)) return [];
  const raw = readFileSync(box.argv, "utf8");
  return raw
    .split("\0\0")
    .filter((block) => block !== "")
    .map((block) => block.split("\0").filter((arg) => arg !== ""));
}

async function runCli(
  box: Sandbox,
  args: string[],
  env: Record<string, string> = {},
): Promise<{ status: number; stdout: string; stderr: string }> {
  const child = Bun.spawn({
    cmd: [process.execPath, CLI, ...args],
    env: { ...box.env, ...env },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = new Response(child.stdout).text();
  const stderr = new Response(child.stderr).text();
  const status = await child.exited;
  return { status, stdout: await stdout, stderr: await stderr };
}

function outputs(box: Sandbox): Record<string, string> {
  const path = join(box.dir, "github-output");
  if (!existsSync(path)) return {};
  return Object.fromEntries(
    readFileSync(path, "utf8")
      .split("\n")
      .filter((line) => line.includes("="))
      .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
  );
}

function options(patch: Partial<Options> = {}): Options {
  return {
    prompt: PROMPT,
    skills: [],
    maxTime: "45m",
    timeout: 3600,
    thoughts: true,
    extraArgs: [],
    dryRun: false,
    ...patch,
  };
}

describe("command line", () => {
  test("builds the unattended invocation", () => {
    const line = buildCommand("/bin/omp", options({ model: "magpie/x", cwd: "/work", approvalMode: "yolo", extraArgs: ["--verbose"] }));
    expect(line).toEqual([
      "/bin/omp",
      "-p",
      "--print-thoughts",
      "--model",
      "magpie/x",
      "--approval-mode",
      "yolo",
      "--no-title",
      "--cwd",
      "/work",
      "--max-time",
      "45m",
      "--verbose",
      PROMPT,
    ]);
  });

  test("never sets a session or a transcript path", () => {
    const line = buildCommand("/bin/omp", options());
    expect(line).not.toContain("--session-dir");
    expect(line).not.toContain("--log");
    expect(line).not.toContain("--no-pty");
    expect(line).not.toContain("--no-lsp");
  });
});

describe("cli", () => {
  test("hands the prompt over verbatim", async () => {
    const box = sandbox();
    const omp = fakeOmp(box);
    const prompt = "line one\nline two\n\n\ttabbed\n";
    const result = await runCli(box, ["--prompt", prompt, "--omp", omp]);

    expect(result.status).toBe(0);
    expect(invocations(box).at(-1)?.at(-1)).toBe(prompt);
  });

  test("echoes the prompt into the log", async () => {
    const box = sandbox();
    const omp = fakeOmp(box, 'echo "thinking: first"\necho "thinking: second"\n');
    const result = await runCli(box, ["--prompt", PROMPT, "--omp", omp]);

    expect(result.stdout).toContain("thinking: first");
    expect(result.stdout).toContain("thinking: second");
    expect(result.stdout).toContain("omp-agent: prompt");
    expect(result.stdout).toContain("fix `strata`");
  });

  test("turns thinking off on request", async () => {
    const box = sandbox();
    const omp = fakeOmp(box);
    await runCli(box, ["--prompt", PROMPT, "--omp", omp, "--no-thoughts"]);

    expect(invocations(box)[0]).not.toContain("--print-thoughts");
  });

  test("takes the model from the environment", async () => {
    const box = sandbox();
    const omp = fakeOmp(box);
    await runCli(box, ["--prompt", PROMPT, "--omp", omp], { OMP_MODEL: "magpie/other" });

    const line = invocations(box)[0];
    expect(line[line.indexOf("--model") + 1]).toBe("magpie/other");
  });

  test("a dry run runs nothing", async () => {
    const box = sandbox();
    const omp = fakeOmp(box);
    const result = await runCli(box, ["--prompt", PROMPT, "--omp", omp, "--dry-run"]);

    expect(result.status).toBe(0);
    expect(invocations(box)).toEqual([]);
    expect(result.stdout).toContain("--print-thoughts");
  });

  test("an empty prompt is a usage error", async () => {
    const box = sandbox();
    const result = await runCli(box, ["--prompt", "  \n", "--omp", fakeOmp(box)]);

    expect(result.status).toBe(2);
  });

  test("a missing binary is 127", async () => {
    const box = sandbox();
    const result = await runCli(box, ["--prompt", PROMPT, "--omp", join(box.dir, "nope")]);

    expect(result.status).toBe(127);
  });

  test("installs skills before the agent", async () => {
    const box = sandbox();
    const omp = fakeOmp(box);
    const result = await runCli(box, [
      "--prompt", PROMPT, "--omp", omp,
      "--skill=@alice/pdf-tools", "--skill=@bob/lint@^2",
    ]);

    expect(result.status).toBe(0);
    expect(invocations(box)).toEqual([
      ["skill", "install", "-y", "-g", "@alice/pdf-tools"],
      ["skill", "install", "-y", "-g", "@bob/lint@^2"],
      buildCommand(omp, options()).slice(1),
    ]);
  });

  test("a skill that will not install stops the run", async () => {
    const box = sandbox();
    const omp = fakeOmp(box, 'if [ "$1" = skill ]; then exit 3; fi\nexit 0\n');
    const result = await runCli(box, ["--prompt", PROMPT, "--omp", omp, "--skill=@alice/pdf-tools"]);

    expect(result.status).toBe(127);
    expect(invocations(box)).toHaveLength(1);
    expect(result.stderr).toContain("could not install skill @alice/pdf-tools");
  });

  test("returns the agent status and publishes it", async () => {
    const box = sandbox();
    const omp = fakeOmp(box, "exit 3\n");
    const result = await runCli(box, ["--prompt", PROMPT, "--omp", omp]);

    expect(result.status).toBe(3);
    expect(outputs(box)).toEqual({ "exit-code": "3" });
  });

  test("kills the agent and its tools on timeout", async () => {
    const box = sandbox();
    const omp = fakeOmp(box, "sleep 60\n");
    const started = Date.now();
    const result = await runCli(box, ["--prompt", PROMPT, "--omp", omp, "--timeout", "1"]);
    const elapsed = Date.now() - started;

    expect(result.status).toBe(124);
    expect(elapsed).toBeLessThan(30_000);
    expect(outputs(box)["exit-code"]).toBe("124");
  });
});

describe("action inputs", () => {
  function inputEnv(box: Sandbox, patch: Record<string, string> = {}): Record<string, string> {
    const env: Record<string, string> = {
      INPUT_PROMPT: PROMPT,
      INPUT_MODEL: "",
      INPUT_CWD: "",
      INPUT_SKILLS: "",
      INPUT_MAX_TIME: "45m",
      INPUT_TIMEOUT: "3600",
      INPUT_APPROVAL_MODE: "yolo",
      INPUT_THOUGHTS: "true",
      INPUT_EXTRA_ARGS: "",
      ...patch,
    };
    // Only create the default fake when the caller did not supply its own: the
    // default would overwrite the override's script on disk.
    if (env.INPUT_OMP === undefined) env.INPUT_OMP = fakeOmp(box);
    return env;
  }

  async function runAction(box: Sandbox, patch: Record<string, string> = {}) {
    const child = Bun.spawn({
      cmd: [process.execPath, CLI],
      env: { ...box.env, ...inputEnv(box, patch) },
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
    });
    const stdout = new Response(child.stdout).text();
    const stderr = new Response(child.stderr).text();
    const status = await child.exited;
    return { status, stdout: await stdout, stderr: await stderr };
  }

  test("defaults match the unattended contract", async () => {
    const box = sandbox();
    const result = await runAction(box);

    expect(result.status).toBe(0);
    expect(invocations(box)).toEqual([[
      "-p",
      "--print-thoughts",
      "--approval-mode",
      "yolo",
      "--no-title",
      "--max-time",
      "45m",
      PROMPT,
    ]]);
  });

  test("inputs reach the command", async () => {
    const box = sandbox();
    const result = await runAction(box, {
      INPUT_MODEL: "magpie/other",
      INPUT_CWD: box.dir,
      INPUT_SKILLS: "@alice/pdf-tools @bob/lint",
      INPUT_MAX_TIME: "10m",
      INPUT_APPROVAL_MODE: "",
      INPUT_THOUGHTS: "false",
      INPUT_EXTRA_ARGS: "--verbose --fast",
    });

    expect(result.status).toBe(0);
    expect(invocations(box)).toEqual([
      ["skill", "install", "-y", "-g", "@alice/pdf-tools"],
      ["skill", "install", "-y", "-g", "@bob/lint"],
      [
        "-p",
        "--model",
        "magpie/other",
        "--no-title",
        "--cwd",
        box.dir,
        "--max-time",
        "10m",
        "--verbose",
        "--fast",
        PROMPT,
      ],
    ]);
  });

  test("the action fails when the agent does", async () => {
    const box = sandbox();
    const result = await runAction(box, { INPUT_OMP: fakeOmp(box, "exit 5\n") });

    expect(result.status).toBe(5);
  });
});