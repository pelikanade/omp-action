#!/usr/bin/env bun
/**
 * Run an omp agent unattended, with its thinking streamed into the job log.
 *
 * `omp -p` prints the agent's final message and drops thinking blocks unless
 * `--print-thoughts` is passed, and an unattended run has no other view of what
 * the agent did. This command builds that invocation and lets the agent write
 * straight into the job log: the job log *is* the transcript.
 *
 * The prompt arrives as text (`INPUT_PROMPT` for the composite action, `--prompt`
 * for a direct run) and is echoed before the agent starts. `--skill` installs a
 * skill with `omp skill install` before the agent starts. Provider configuration
 * stays where omp reads it: `PI_CONFIG_FILES` for extensions such as
 * `omp-magpie`, and the files those extensions read. No provider secret is
 * passed on the command line or stored in GitHub Actions.
 *
 * Nothing outside Bun and Node's standard library is used; the action runs this
 * file with `bun`, so there is no build step and no bundle.
 *
 * Exit status: the agent's own status, 124 when the timeout killed it, 127 when
 * the agent never started (no omp binary, or a skill that would not install),
 * and 2 when the command line is wrong.
 */

import { appendFileSync } from "node:fs";
import { parseArgs } from "node:util";

const TIMEOUT_STATUS = 124;
const NO_AGENT_STATUS = 127;
const USAGE_STATUS = 2;
const DEFAULT_MAX_TIME = "45m";
const DEFAULT_TIMEOUT = 3600;

export interface Options {
  prompt: string;
  model?: string;
  omp?: string;
  cwd?: string;
  skills: string[];
  maxTime: string;
  timeout: number;
  approvalMode?: string;
  thoughts: boolean;
  extraArgs: string[];
  dryRun: boolean;
}

function publish(name: string, value: string): void {
  const path = process.env.GITHUB_OUTPUT;
  if (path) appendFileSync(path, `${name}=${value}\n`);
}

export function buildCommand(omp: string, options: Options): string[] {
  const line = [omp, "-p"];
  if (options.thoughts) line.push("--print-thoughts");
  if (options.model) line.push("--model", options.model);
  if (options.approvalMode) line.push("--approval-mode", options.approvalMode);
  // A generated session title has nowhere to be shown in an unattended run.
  line.push("--no-title");
  if (options.cwd) line.push("--cwd", options.cwd);
  if (options.maxTime) line.push("--max-time", options.maxTime);
  line.push(...options.extraArgs, options.prompt);
  return line;
}

function killGroup(pid: number): void {
  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // already gone
    }
  }
}

/** Run a command with inherited stdio, killing its whole session after `timeout` seconds. */
async function run(command: string[], cwd: string, timeout: number): Promise<number> {
  const child = Bun.spawn({
    cmd: ["setsid", ...command],
    cwd,
    stdin: "ignore",
    stdout: "inherit",
    stderr: "inherit",
  });
  let killed = false;
  const timer = setTimeout(() => {
    killed = true;
    killGroup(child.pid);
  }, timeout * 1000);
  const status = await child.exited;
  clearTimeout(timer);
  return killed ? TIMEOUT_STATUS : status;
}

async function installSkills(
  omp: string,
  skills: string[],
  cwd: string,
  timeout: number,
): Promise<number> {
  // User-global on purpose: the workspace, and the diff a review reads, stay
  // untouched.
  for (const spec of skills) {
    console.log(`omp-agent: installing skill ${spec}`);
    const status = await run([omp, "skill", "install", "-y", "-g", spec], cwd, timeout);
    if (status !== 0) {
      console.error(`omp-agent: could not install skill ${spec} (exit ${status})`);
      return NO_AGENT_STATUS;
    }
  }
  return 0;
}

function fromInputs(): Options {
  const read = (name: string): string =>
    process.env[`INPUT_${name.toUpperCase().replaceAll("-", "_")}`] ?? "";
  return {
    prompt: read("prompt"),
    model: read("model") || undefined,
    omp: read("omp") || undefined,
    cwd: read("cwd") || undefined,
    skills: read("skills").split(/\s+/).filter(Boolean),
    maxTime: read("max-time") || DEFAULT_MAX_TIME,
    timeout: Number(read("timeout") || DEFAULT_TIMEOUT),
    approvalMode: read("approval-mode") || undefined,
    thoughts: read("thoughts") !== "false",
    extraArgs: read("extra-args").split(/\s+/).filter(Boolean),
    dryRun: false,
  };
}

function fromArgv(argv: string[]): Options {
  const { values } = parseArgs({
    args: argv,
    options: {
      prompt: { type: "string" },
      model: { type: "string" },
      omp: { type: "string" },
      cwd: { type: "string" },
      skill: { type: "string", multiple: true, default: [] },
      "max-time": { type: "string", default: DEFAULT_MAX_TIME },
      timeout: { type: "string", default: String(DEFAULT_TIMEOUT) },
      "approval-mode": { type: "string" },
      "no-thoughts": { type: "boolean", default: false },
      "extra-arg": { type: "string", multiple: true, default: [] },
      "dry-run": { type: "boolean", default: false },
    },
    allowPositionals: false,
  });
  return {
    prompt: values.prompt ?? "",
    model: values.model,
    omp: values.omp,
    cwd: values.cwd,
    skills: values.skill,
    maxTime: values["max-time"],
    timeout: Number(values.timeout),
    approvalMode: values["approval-mode"],
    thoughts: !values["no-thoughts"],
    extraArgs: values["extra-arg"],
    dryRun: values["dry-run"],
  };
}

async function main(options: Options): Promise<number> {
  if (options.prompt.trim() === "") {
    console.error("omp-agent: the prompt is empty");
    return USAGE_STATUS;
  }
  const omp = options.omp ?? process.env.OMP_AGENT_BIN ?? Bun.which("omp");
  if (!omp) {
    console.error("omp-agent: omp is not on PATH; pass --omp or set OMP_AGENT_BIN");
    return NO_AGENT_STATUS;
  }
  const model = options.model ?? process.env.OMP_MODEL ?? undefined;
  const cwd = options.cwd ?? process.cwd();
  const resolved: Options = { ...options, model };

  if (!options.dryRun) {
    const status = await installSkills(omp, options.skills, cwd, options.timeout);
    if (status !== 0) return status;
  }

  const line = buildCommand(omp, resolved);
  const shown = line.slice(0, -1).join(" ");
  console.log(`omp-agent: ${shown} <prompt: ${options.prompt.length} chars>`);
  console.log("omp-agent: prompt");
  console.log(options.prompt);
  console.log("omp-agent: end of prompt");
  if (options.dryRun) return 0;

  const status = await run(line, cwd, options.timeout);
  publish("exit-code", String(status));
  if (status !== 0) console.error(`omp-agent: the agent exited ${status}`);
  return status;
}

if (import.meta.main) {
  const options = process.env.INPUT_PROMPT !== undefined ? fromInputs() : fromArgv(Bun.argv.slice(2));
  process.exit(await main(options));
}