"""The offline parser and the REPL stay on their existing path."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import config
import substrate
import version_store as vs
from lexicon import Lexicon
from parser import ParserContext, RuleParser
from state import SessionState

ROOT = Path(__file__).resolve().parents[1]


def _context() -> ParserContext:
    return ParserContext(
        intent_mode="ideation",
        risk_scope="dev",
        focus_key="root",
        version_id="v0.0.0",
        branch="main",
        lexicon_text="(empty — no terms have been coined yet)",
        model_summary="(empty — nothing modelled yet)",
    )


class RuleParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RuleParser()
        self.ctx = _context()

    def test_add_node_edge_coinage_and_commands(self):
        added = self.parser.parse("add a component called ignition", self.ctx)
        self.assertEqual(added.model_ops[0]["op"], "add_node")
        self.assertEqual(added.model_ops[0]["id"], "ignition")
        self.assertEqual(added.source, "rules")

        linked = self.parser.parse("ignition feeds into cooling", self.ctx)
        self.assertEqual(linked.model_ops[0]["op"], "add_edge")
        self.assertEqual(linked.model_ops[0]["src"], "ignition")
        self.assertEqual(linked.model_ops[0]["dst"], "cooling")

        coined = self.parser.parse(
            "when I say a spine, I mean the load-bearing path through the system",
            self.ctx,
        )
        self.assertIn("spine", coined.coined_terms[0]["term"])
        self.assertIn("load-bearing", coined.coined_terms[0]["means"])

        locked = self.parser.parse("lock it in", self.ctx)
        self.assertEqual(locked.version_action, "lock_in")

        walked = self.parser.parse("walk it back to 0.0.2", self.ctx)
        self.assertEqual(walked.version_action, "walk_back")
        self.assertEqual(walked.version_arg, "0.0.2")

        self.assertEqual(self.parser.parse("quit", self.ctx).system_command, "quit")
        self.assertEqual(self.parser.name, "rules")


class ReplTests(unittest.TestCase):
    def setUp(self):
        self._stdout = io.StringIO()
        self._redirect = redirect_stdout(self._stdout)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)

    def test_run_turn_still_commits_and_quits(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = config.paths(Path(tmp))
            paths["workspace"].mkdir(parents=True, exist_ok=True)
            store = vs.make_store("jsonlog", paths["versions"])
            store.init()
            state = SessionState(assume_yes=True)
            lex = Lexicon(paths["lexicon"])
            parser = RuleParser()
            ctx = (store, state, lex, parser, paths)

            exited = substrate.run_turn("add a component called ignition", ctx)
            self.assertFalse(exited)
            self.assertIn("ignition", store.current()["nodes"])
            self.assertEqual(store.current_version(), "v0.0.1")
            written = list(paths["artifacts"].rglob("model.json"))
            self.assertEqual(len(written), 1)
            self.assertIn("ignition", json.loads(written[0].read_text(encoding="utf-8"))["nodes"])

            self.assertTrue(substrate.run_turn("quit", ctx))

    def test_cli_repl_dry_run_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "substrate.py"),
                    "--dry-run",
                    "--backend",
                    "jsonlog",
                    "--workspace",
                    tmp,
                    "--yes",
                ],
                input="add a component called ignition\nquit\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ignition", result.stdout)
            self.assertTrue((Path(tmp) / "artifacts" / "model.json").is_file())

    def test_cli_agent_usage(self):
        dry = subprocess.run(
            [sys.executable, str(ROOT / "substrate.py"), "--dry-run", "agent", "map the cooling loop"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(dry.returncode, 2, dry.stderr)
        self.assertIn("agent mode", dry.stdout)

        missing = subprocess.run(
            [sys.executable, str(ROOT / "substrate.py"), "agent"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn("needs a goal", missing.stdout)

        capped = subprocess.run(
            [sys.executable, str(ROOT / "substrate.py"), "agent", "map it", "--max-steps", "0"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(capped.returncode, 2, capped.stderr)
        self.assertIn("--max-steps", capped.stdout)

        help_text = subprocess.run(
            [sys.executable, str(ROOT / "substrate.py"), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        self.assertIn("agent", help_text.stdout)
        self.assertIn("--max-steps", help_text.stdout)

    def test_argparse_accepts_the_agent_command(self):
        parser = substrate.build_arg_parser()
        args = parser.parse_args(
            ["--backend", "jsonlog", "agent", "map the cooling loop", "--max-steps", "7"]
        )
        self.assertEqual(args.command, "agent")
        self.assertEqual(args.goal, "map the cooling loop")
        self.assertEqual(args.max_steps, 7)
        self.assertEqual(args.backend, "jsonlog")

        repl = parser.parse_args(["--dry-run"])
        self.assertIsNone(repl.command)
        self.assertIsNone(repl.goal)
        self.assertEqual(repl.max_steps, config.AGENT_MAX_STEPS)


if __name__ == "__main__":
    unittest.main()
