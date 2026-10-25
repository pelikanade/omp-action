#!/usr/bin/env python3
"""Tests for the omp-agent command and for the action's input mapping."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The Nix sandbox has no /usr/bin/env, so the fake agents get the interpreter's
# own path when the suite can find it.
SHEBANG = f"#!{shutil.which('bash') or '/usr/bin/env bash'}"

import omp_agent  # noqa: E402

PROMPT = "# Repair the failed update\n\n- fix `strata`\n"


class Sandbox(unittest.TestCase):
    """A temporary workspace with a fake omp that records how it was called."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.argv = self.dir / "argv.bin"
        self.github_output = self.dir / "github-output"
        # Empty the variables the command reads, so the suite is independent of
        # the environment it runs in.
        env = mock.patch.dict(
            os.environ,
            {
                "FAKE_ARGV": str(self.argv),
                "GITHUB_OUTPUT": str(self.github_output),
                "OMP_MODEL": "",
                "OMP_AGENT_BIN": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def fake_omp(self, body: str = "exit 0\n") -> str:
        """A fake omp that appends its arguments, NUL separated, to $FAKE_ARGV."""
        path = self.dir / "fake-omp"
        path.write_text(
            SHEBANG
            + "\n"
            + 'printf "%s\\0" "$@" >> "$FAKE_ARGV"\n'
            + 'printf "\\0" >> "$FAKE_ARGV"\n'
            + body
        )
        path.chmod(0o755)
        return str(path)

    def invocations(self) -> list[list[str]]:
        if not self.argv.exists():
            return []
        raw = self.argv.read_bytes().decode(errors="replace")
        return [
            [arg for arg in block.split("\0") if arg]
            for block in raw.split("\0\0")
            if block
        ]

    def outputs(self) -> dict[str, str]:
        if not self.github_output.exists():
            return {}
        return dict(
            line.split("=", 1)
            for line in self.github_output.read_text().splitlines()
            if "=" in line
        )

    def cli(self, *args: str, prompt: str = PROMPT) -> int:
        """Run the command in process, keeping its output for the assertions."""
        self._stdout, self._stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(self._stdout), redirect_stderr(self._stderr):
            return omp_agent.main(["--prompt", prompt, *args])

    def console(self) -> str:
        return self._stdout.getvalue()

    def errors(self) -> str:
        return self._stderr.getvalue()


class CommandLine(Sandbox):
    def test_builds_the_unattended_invocation(self) -> None:
        omp = self.fake_omp()
        status = self.cli(
            "--omp", omp,
            "--model", "magpie/deepseek/deepseek-flash",
            "--cwd", str(self.dir),
            "--max-time", "10m",
            "--approval-mode", "yolo",
            "--extra-arg=--verbose",
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            self.invocations(),
            [[
                "-p",
                "--print-thoughts",
                "--model",
                "magpie/deepseek/deepseek-flash",
                "--approval-mode",
                "yolo",
                "--no-title",
                "--cwd",
                str(self.dir),
                "--max-time",
                "10m",
                "--verbose",
                PROMPT,
            ]],
        )

    def test_prompt_is_handed_over_verbatim(self) -> None:
        omp = self.fake_omp()
        prompt = "line one\nline two\n\n\ttabbed\n"
        self.cli("--omp", omp, prompt=prompt)

        self.assertEqual(self.invocations()[0][-1], prompt)

    def test_no_session_or_log_or_tool_flags(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp)

        arguments = " ".join(self.invocations()[0])
        for flag in ["--session-dir", "--log", "--no-skills", "--no-pty", "--no-lsp"]:
            self.assertNotIn(flag, arguments)

    def test_thinking_can_be_turned_off(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp, "--no-thoughts")

        self.assertNotIn("--print-thoughts", self.invocations()[0])

    def test_model_comes_from_the_environment_when_not_given(self) -> None:
        omp = self.fake_omp()
        with mock.patch.dict(os.environ, {"OMP_MODEL": "magpie/other"}):
            self.cli("--omp", omp)

        arguments = self.invocations()[0]
        self.assertEqual(arguments[arguments.index("--model") + 1], "magpie/other")

    def test_no_model_flag_when_nothing_asks_for_one(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp)

        self.assertNotIn("--model", self.invocations()[0])

    def test_dry_run_runs_nothing(self) -> None:
        omp = self.fake_omp()
        status = self.cli("--omp", omp, "--dry-run")

        self.assertEqual(status, 0)
        self.assertEqual(self.invocations(), [])
        self.assertIn("--print-thoughts", self.console())
        self.assertIn("end of prompt", self.console())

    def test_empty_prompt_is_a_usage_error(self) -> None:
        status = self.cli(prompt="   \n")

        self.assertEqual(status, omp_agent.USAGE_STATUS)

    def test_missing_binary(self) -> None:
        status = self.cli("--omp", str(self.dir / "not-here"))

        self.assertEqual(status, omp_agent.NO_AGENT_STATUS)


class Skills(Sandbox):
    def test_skills_are_installed_before_the_agent(self) -> None:
        omp = self.fake_omp()
        status = self.cli("--omp", omp, "--skill=@alice/pdf-tools", "--skill=@bob/lint@^2")

        self.assertEqual(status, 0)
        self.assertEqual(
            self.invocations(),
            [
                ["skill", "install", "-y", "-g", "@alice/pdf-tools"],
                ["skill", "install", "-y", "-g", "@bob/lint@^2"],
                ["-p", "--print-thoughts", "--no-title", "--max-time", "45m", PROMPT],
            ],
        )

    def test_a_skill_that_will_not_install_stops_the_run(self) -> None:
        omp = self.fake_omp('if [ "$1" = skill ]; then exit 3; fi\nexit 0\n')
        status = self.cli("--omp", omp, "--skill=@alice/pdf-tools")

        self.assertEqual(status, omp_agent.NO_AGENT_STATUS)
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("could not install skill @alice/pdf-tools", self.errors())


class Streaming(Sandbox):
    def test_streams_the_output_and_the_prompt(self) -> None:
        omp = self.fake_omp('echo "thinking: first"\necho "thinking: second"\n')
        status = self.cli("--omp", omp)

        self.assertEqual(status, 0)
        self.assertIn("thinking: first", self.console())
        self.assertIn("thinking: second", self.console())
        self.assertIn("omp-agent: prompt", self.console())
        self.assertIn("fix `strata`", self.console())

    def test_returns_the_agent_status_and_publishes_it(self) -> None:
        omp = self.fake_omp("exit 3\n")
        status = self.cli("--omp", omp)

        self.assertEqual(status, 3)
        self.assertEqual(self.outputs(), {"exit-code": "3"})
        self.assertIn("the agent exited 3", self.errors())

    def test_kills_a_hung_agent_and_its_tools(self) -> None:
        omp = self.fake_omp("sleep 60\n")
        started = time.monotonic()
        status = self.cli("--omp", omp, "--timeout", "1")
        elapsed = time.monotonic() - started

        self.assertEqual(status, omp_agent.TIMEOUT_STATUS)
        self.assertLess(elapsed, 30)
        self.assertIn("killed after 1s", self.errors())
        self.assertEqual(self.outputs()["exit-code"], str(omp_agent.TIMEOUT_STATUS))


class ActionMapping(Sandbox):
    """The composite action's inputs, as a workflow would set them."""

    def run_action(self, **overrides: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GITHUB_ACTION_PATH": str(ROOT),
            "INPUT_PROMPT": PROMPT,
            "INPUT_MODEL": "",
            "INPUT_CWD": "",
            "INPUT_SKILLS": "",
            "INPUT_MAX_TIME": "45m",
            "INPUT_TIMEOUT": "3600",
            "INPUT_APPROVAL_MODE": "yolo",
            "INPUT_THOUGHTS": "true",
            "INPUT_EXTRA_ARGS": "",
            **overrides,
        }
        # The default must not overwrite a fake an override already created.
        if "INPUT_OMP" not in env:
            env["INPUT_OMP"] = self.fake_omp()
        return subprocess.run(
            ["bash", str(ROOT / "action.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_defaults_match_the_unattended_contract(self) -> None:
        action = self.run_action()

        self.assertEqual(action.returncode, 0, action.stderr)
        self.assertEqual(
            self.invocations(),
            [[
                "-p",
                "--print-thoughts",
                "--approval-mode",
                "yolo",
                "--no-title",
                "--max-time",
                "45m",
                PROMPT,
            ]],
        )

    def test_inputs_reach_the_command(self) -> None:
        action = self.run_action(
            INPUT_MODEL="magpie/other",
            INPUT_CWD=str(self.dir),
            INPUT_SKILLS="@alice/pdf-tools @bob/lint",
            INPUT_MAX_TIME="10m",
            INPUT_TIMEOUT="60",
            INPUT_APPROVAL_MODE="",
            INPUT_THOUGHTS="false",
            INPUT_EXTRA_ARGS="--verbose --fast",
        )

        self.assertEqual(action.returncode, 0, action.stderr)
        self.assertEqual(
            self.invocations(),
            [
                ["skill", "install", "-y", "-g", "@alice/pdf-tools"],
                ["skill", "install", "-y", "-g", "@bob/lint"],
                [
                    "-p",
                    "--model",
                    "magpie/other",
                    "--no-title",
                    "--cwd",
                    str(self.dir),
                    "--max-time",
                    "10m",
                    "--verbose",
                    "--fast",
                    PROMPT,
                ],
            ],
        )

    def test_the_action_fails_when_the_agent_does(self) -> None:
        action = self.run_action(INPUT_OMP=self.fake_omp("exit 5\n"))

        self.assertEqual(action.returncode, 5)


if __name__ == "__main__":
    unittest.main()
