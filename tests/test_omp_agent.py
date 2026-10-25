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


def executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


class Sandbox(unittest.TestCase):
    """A temporary workspace with a fake omp that records its arguments."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.prompt = self.dir / "prompt.md"
        self.prompt.write_text("# prompt\n")
        self.log = self.dir / "logs" / "omp.log"
        self.argv = self.dir / "argv.txt"
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
        return str(
            executable(
                self.dir / "fake-omp",
                SHEBANG + "\n" + 'printf \'%s\\n\' "$@" > "$FAKE_ARGV"\n' + body,
            )
        )

    def arguments(self) -> list[str]:
        return self.argv.read_text().splitlines()

    def outputs(self) -> dict[str, str]:
        if not self.github_output.exists():
            return {}
        return dict(
            line.split("=", 1)
            for line in self.github_output.read_text().splitlines()
            if "=" in line
        )

    def cli(self, *args: str) -> int:
        return omp_agent.main(
            ["--prompt", str(self.prompt), "--log", str(self.log), *args]
        )


class CommandLine(Sandbox):
    def test_builds_the_unattended_invocation(self) -> None:
        omp = self.fake_omp()
        status = self.cli(
            "--omp", omp,
            "--model", "magpie/deepseek/deepseek-flash",
            "--cwd", str(self.dir),
            "--session-dir", str(self.dir / "sessions"),
            "--max-time", "10m",
            "--approval-mode", "yolo",
            "--no-skills", "--no-pty", "--no-lsp", "--no-title",
            "--extra-arg=--verbose",
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            self.arguments(),
            [
                "-p",
                "--print-thoughts",
                "--model",
                "magpie/deepseek/deepseek-flash",
                "--approval-mode",
                "yolo",
                "--no-skills",
                "--no-pty",
                "--no-lsp",
                "--no-title",
                "--cwd",
                str(self.dir),
                "--session-dir",
                str(self.dir / "sessions"),
                "--max-time",
                "10m",
                "--verbose",
                f"@{self.prompt}",
            ],
        )

    def test_default_session_dir_sits_next_to_the_log(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp)

        arguments = self.arguments()
        self.assertEqual(
            arguments[arguments.index("--session-dir") + 1],
            str(self.log.parent / "sessions"),
        )

    def test_thinking_can_be_turned_off(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp, "--no-thoughts")

        self.assertNotIn("--print-thoughts", self.arguments())

    def test_model_comes_from_the_environment_when_not_given(self) -> None:
        omp = self.fake_omp()
        with mock.patch.dict(os.environ, {"OMP_MODEL": "magpie/other"}):
            self.cli("--omp", omp)

        arguments = self.arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "magpie/other")

    def test_no_model_flag_when_nothing_asks_for_one(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp)

        self.assertNotIn("--model", self.arguments())

    def test_dry_run_touches_nothing(self) -> None:
        omp = self.fake_omp()
        with redirect_stdout(io.StringIO()) as printed:
            status = self.cli("--omp", omp, "--dry-run")

        self.assertEqual(status, 0)
        self.assertFalse(self.argv.exists())
        self.assertFalse(self.log.exists())
        self.assertIn("--print-thoughts", printed.getvalue())


class Streaming(Sandbox):
    def test_streams_to_the_console_and_keeps_the_transcript(self) -> None:
        omp = self.fake_omp('echo "thinking: first"\necho "thinking: second"\n')
        # A console without a byte buffer is the case that loses a transcript
        # if the log write is not the first thing the tee does.
        with redirect_stdout(io.StringIO()) as printed:
            status = self.cli("--omp", omp)

        self.assertEqual(status, 0)
        self.assertIn("thinking: first", printed.getvalue())
        self.assertEqual(
            self.log.read_text().splitlines(),
            ["thinking: first", "thinking: second"],
        )

    def test_returns_the_agent_status(self) -> None:
        omp = self.fake_omp("exit 3\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = self.cli("--omp", omp)

        self.assertEqual(status, 3)
        self.assertEqual(self.outputs()["exit-code"], "3")

    def test_publishes_the_transcript_and_session_paths(self) -> None:
        omp = self.fake_omp()
        self.cli("--omp", omp)

        outputs = self.outputs()
        self.assertEqual(outputs["log"], str(self.log))
        self.assertEqual(outputs["session-dir"], str(self.log.parent / "sessions"))

    def test_kills_a_hung_agent(self) -> None:
        omp = self.fake_omp("sleep 60\n")
        started = time.monotonic()
        status = self.cli("--omp", omp, "--timeout", "1")
        elapsed = time.monotonic() - started

        self.assertEqual(status, omp_agent.TIMEOUT_STATUS)
        self.assertLess(elapsed, 30)
        self.assertIn("killed the agent after 1s", self.log.read_text())
        self.assertEqual(self.outputs()["exit-code"], str(omp_agent.TIMEOUT_STATUS))

    def test_missing_binary(self) -> None:
        status = self.cli("--omp", str(self.dir / "not-here"))

        self.assertEqual(status, omp_agent.MISSING_STATUS)

    def test_missing_prompt(self) -> None:
        status = omp_agent.main(
            ["--prompt", str(self.dir / "not-here.md"), "--log", str(self.log)]
        )

        self.assertEqual(status, omp_agent.USAGE_STATUS)


class ActionMapping(Sandbox):
    """The composite action's inputs, as a workflow would set them."""

    def run_action(self, **overrides: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GITHUB_ACTION_PATH": str(ROOT),
            "INPUT_PROMPT": str(self.prompt),
            "INPUT_LOG": str(self.log),
            "INPUT_MODEL": "",
            "INPUT_CWD": "",
            "INPUT_SESSION_DIR": "",
            "INPUT_MAX_TIME": "45m",
            "INPUT_TIMEOUT": "3600",
            "INPUT_APPROVAL_MODE": "yolo",
            "INPUT_THOUGHTS": "true",
            "INPUT_NO_SKILLS": "true",
            "INPUT_NO_PTY": "true",
            "INPUT_NO_LSP": "true",
            "INPUT_NO_TITLE": "true",
            "INPUT_EXTRA_ARGS": "",
            **overrides,
        }
        # setdefault would evaluate its default eagerly and overwrite an
        # override's fake with the exit-0 one.
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
            self.arguments(),
            [
                "-p",
                "--print-thoughts",
                "--approval-mode",
                "yolo",
                "--no-skills",
                "--no-pty",
                "--no-lsp",
                "--no-title",
                "--session-dir",
                str(self.log.parent / "sessions"),
                "--max-time",
                "45m",
                f"@{self.prompt}",
            ],
        )

    def test_inputs_reach_the_command(self) -> None:
        action = self.run_action(
            INPUT_MODEL="magpie/other",
            INPUT_CWD=str(self.dir),
            INPUT_MAX_TIME="10m",
            INPUT_TIMEOUT="60",
            INPUT_APPROVAL_MODE="",
            INPUT_THOUGHTS="false",
            INPUT_NO_SKILLS="false",
            INPUT_EXTRA_ARGS="--verbose --fast",
        )

        self.assertEqual(action.returncode, 0, action.stderr)
        self.assertEqual(
            self.arguments(),
            [
                "-p",
                "--model",
                "magpie/other",
                "--no-pty",
                "--no-lsp",
                "--no-title",
                "--cwd",
                str(self.dir),
                "--session-dir",
                str(self.log.parent / "sessions"),
                "--max-time",
                "10m",
                "--verbose",
                "--fast",
                f"@{self.prompt}",
            ],
        )

    def test_the_action_fails_when_the_agent_does(self) -> None:
        action = self.run_action(INPUT_OMP=self.fake_omp("exit 5\n"))

        self.assertEqual(action.returncode, 5)


if __name__ == "__main__":
    unittest.main()
