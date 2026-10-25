# omp-action

Run an [omp](https://github.com/can1357/oh-my-pi) agent unattended, with its
thinking streamed into the job log.

`omp -p` prints the agent's final message and drops thinking blocks unless
`--print-thoughts` is passed, and an unattended run has no other view of what the
agent did. This repository is one TypeScript command that asks for the thinking
and lets the agent write straight into the job log — the job log *is* the
transcript — plus the composite action that runs it as a workflow step.

There is no build step and no bundle: the action runs `omp_agent.ts` with `bun`,
using only Bun and Node's standard library, so `bun` and `omp` must be on
`PATH`. The consuming repository's runner provisioning installs both (in a Nix
flake, `pkgs.bun` and the locked omp package).

## As an action

```yaml
- name: Repair the failed updates
  id: repair
  uses: pelikanade/omp-action@v1
  with:
    prompt: |
      Repair the failed package updates. The logs are in .ci/logs.
    model: ${{ vars.OMP_MODEL }}
```

| Input | Default | Meaning |
| --- | --- | --- |
| `prompt` | — | the agent's prompt, as text (required) |
| `model` | | model to run; empty uses omp's own default |
| `omp` | | omp binary; empty uses `$OMP_AGENT_BIN`, then `omp` on `PATH` |
| `cwd` | | directory the agent works in; empty uses the job's working directory |
| `skills` | | skills to install with `omp skill install` before the agent starts, in that command's spec format (`@scope/name[@version]`); whitespace separated |
| `max-time` | `45m` | omp's own session limit |
| `timeout` | `3600` | seconds after which the agent and the tools it started are killed |
| `approval-mode` | `yolo` | an unattended step cannot answer approval prompts |
| `thoughts` | `true` | include thinking blocks in the job log |
| `extra-args` | | extra arguments for the omp command, split on spaces |

| Output | Meaning |
| --- | --- |
| `exit-code` | the agent's status; `124` when the timeout killed it, `127` when it never started |

The step fails when the agent exits non-zero. Add `continue-on-error: true`
when the caller wants to read `steps.<id>.outputs.exit-code` itself, which is
what a phase that verifies the agent's result does.

## Running it directly

The same file is the command, for a pipeline that needs the agent inside a phase
of its own:

```sh
bun omp_agent.ts \
  --prompt "$(cat prompt.md)" --cwd "$PWD" \
  --model magpie/deepseek/deepseek-flash \
  --max-time 45m --approval-mode yolo
```

`--help` lists every option, including `--skill` for skills to install first,
`--no-thoughts`, `--extra-arg` for anything else, and `--dry-run` to print the
command and the prompt without running anything.

## What the command deliberately does not configure

- **No prompt file.** The prompt is text on the command line, and it is echoed
  into the job log before the agent starts, so the step log shows exactly what
  the agent was asked.
- **No transcript file.** The agent inherits the step's output, so its thinking
  lands in the job log as it arrives; GitHub keeps that log and its raw download.
- **No session directory.** Sessions go wherever omp keeps them; nothing in the
  caller's workspace is written.
- **No `--no-pty` or `--no-lsp`.** omp's own defaults stand: this tool overrides
  only what an unattended run needs (`-p`, `--print-thoughts`, `--no-title`).
- **`--no-title` always.** A generated session title has nowhere to be shown in
  an unattended run.

## `max-time` and `timeout`

They are different mechanisms and both are kept:

- `--max-time` is passed *to omp*: omp stops its own session at that point and
  winds down deliberately.
- `--timeout` is this tool's hard wall: at that point the agent's whole session
  is killed (exit `124`), including the tools it started.

`--timeout` is the backstop for an omp that hangs, ignores its own limit, or
leaves a child process running, so keep it above `--max-time`. A `nix build`
that a repair agent started cannot outlive the step.

## Skills

`--skill` (the action's `skills`) takes the specs `omp skill install` accepts —
`@scope/name`, `@scope/name@version`, `@scope/name@range`. Each one is installed
with `omp skill install -y -g` **before** the agent starts, into the user-global
set, so the working tree and the diff under review stay untouched. A skill that
will not install stops the run with exit `127`: the agent never starts with a
half-installed skill set.

## Provider configuration

The command passes no credentials. omp reads its extensions from
`PI_CONFIG_FILES`, and those extensions read their own settings — a Magpie
gateway, for example, comes from the `omp-magpie` extension plus
`~/.omp/agent/magpie.yaml` with the endpoint and key, which a runner can render
from its own secret store. Nothing provider-specific is stored in the workflow.

## Exit status

| Status | Meaning |
| --- | --- |
| *the agent's own* | the agent ran to completion and exited with this status |
| `124` | `--timeout` killed the agent and its tools |
| `127` | the agent never started: no omp binary, or a skill that would not install |
| `2` | the command line is wrong, for example an empty prompt |

## Development

```sh
bun test
```

`tests/omp_agent.test.ts` runs the command against a fake `omp` that records
every invocation (including `omp skill install`, NUL separated so the order is
checkable), and runs it with the same `INPUT_` variables a workflow sets, so the
action's input mapping is verified without a workflow run. The timeout test also
checks that the agent's own child processes die with it.