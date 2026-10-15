"""Agent mode with the model stubbed. No network, no API key."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent
import config
import version_store as vs
from agent import AgentSession, ModelTurn, ToolUse, audit_graph, exit_code
from state import SessionState


def _node(nid: str, **extra) -> dict:
    node = {
        "id": nid,
        "label": extra.get("label", nid),
        "kind": extra.get("kind", "component"),
        "description": extra.get("description", ""),
        "attrs": {},
        "tags": [],
        "focus": "root",
    }
    node.update(extra)
    return node


def _model(nodes: dict, edges: list | None = None) -> dict:
    return {"meta": {}, "nodes": nodes, "edges": edges or [], "notes": []}


class ScriptedClient:
    """Returns a fixed sequence of turns. Raises if the loop asks for more."""

    def __init__(self, turns: list[ModelTurn]):
        self.turns = list(turns)
        self.seen: list[list] = []
        self.systems: list[str] = []

    def complete(self, *, system, messages, tools):
        self.systems.append(system)
        self.seen.append(messages)
        if not self.turns:
            raise AssertionError("model was called more times than the script")
        return self.turns.pop(0)


def _use(tool_id: str, name: str, arguments: dict | None = None) -> ToolUse:
    return ToolUse(id=tool_id, name=name, arguments=arguments or {})


def _session(root: Path, **state_kwargs) -> AgentSession:
    paths = config.paths(root / "ws")
    paths["workspace"].mkdir(parents=True, exist_ok=True)
    store = vs.make_store("jsonlog", paths["versions"])
    store.init()
    return AgentSession(
        store=store,
        state=SessionState(**state_kwargs),
        paths=paths,
        goal="map the cooling loop",
        read_roots=[paths["workspace"]],
    )


def _kinds(model: dict) -> set[str]:
    return {gap["kind"] for gap in audit_graph(model)}


class AuditTests(unittest.TestCase):
    def test_empty_and_single_node_are_clear(self):
        self.assertEqual(audit_graph(_model({})), [])
        self.assertEqual(audit_graph(_model({"ignition": _node("ignition")})), [])

    def test_orphans_once_two_nodes_exist(self):
        gaps = audit_graph(
            _model({"ignition": _node("ignition"), "cooling": _node("cooling")})
        )
        self.assertEqual(_kinds(_model({"ignition": _node("ignition"), "cooling": _node("cooling")})), {"orphan"})
        details = " ".join(gap["detail"] for gap in gaps)
        self.assertIn("ignition", details)
        self.assertIn("cooling", details)

    def test_description_claims_a_missing_edge(self):
        model = _model(
            {
                "ignition": _node("ignition", description="ignition feeds cooling"),
                "cooling": _node("cooling", description="takes heat"),
            }
        )
        gaps = [gap for gap in audit_graph(model) if gap["kind"] == "missing_edge"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("ignition", gaps[0]["detail"])
        self.assertIn("cooling", gaps[0]["detail"])

    def test_negated_relation_is_not_a_missing_edge(self):
        model = _model(
            {
                "ignition": _node(
                    "ignition", description="ignition is not connected to cooling"
                ),
                "cooling": _node("cooling"),
            }
        )
        self.assertNotIn("missing_edge", _kinds(model))

    def test_linked_nodes_are_clear(self):
        model = _model(
            {
                "ignition": _node("ignition", description="ignition feeds into cooling"),
                "cooling": _node("cooling", description="takes heat off the ignition path"),
            },
            [{"src": "ignition", "dst": "cooling", "label": "feeds into"}],
        )
        self.assertEqual(audit_graph(model), [])

    def test_dangling_self_and_opposing_edges(self):
        dangling = _model(
            {"ignition": _node("ignition")},
            [{"src": "ignition", "dst": "missing", "label": "feeds"}],
        )
        self.assertIn("missing_edge", _kinds(dangling))

        self_edge = _model(
            {"ignition": _node("ignition")},
            [{"src": "ignition", "dst": "ignition", "label": "feeds"}],
        )
        self.assertIn("contradiction", _kinds(self_edge))

        opposing = _model(
            {"ignition": _node("ignition"), "cooling": _node("cooling")},
            [
                {"src": "ignition", "dst": "cooling", "label": "feeds"},
                {"src": "ignition", "dst": "cooling", "label": "not feeds"},
            ],
        )
        contradictions = [gap for gap in audit_graph(opposing) if gap["kind"] == "contradiction"]
        self.assertTrue(contradictions)
        self.assertIn("feeds", contradictions[0]["detail"])

    def test_duplicate_labels(self):
        model = _model(
            {
                "ignition": _node("ignition", label="Ignition"),
                "spark": _node("spark", label="ignition"),
            },
            [{"src": "ignition", "dst": "spark", "label": "starts"}],
        )
        gaps = [gap for gap in audit_graph(model) if gap["kind"] == "contradiction"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("share the label", gaps[0]["detail"])

    def test_implied_node_is_incomplete(self):
        model = _model(
            {
                "cooling": _node(
                    "cooling",
                    kind="implied",
                    description="implied by an edge before it was described",
                )
            }
        )
        self.assertEqual(_kinds(model), {"incomplete"})


class ToolAndLoopTests(unittest.TestCase):
    def setUp(self):
        self._stdout = io.StringIO()
        self._redirect = redirect_stdout(self._stdout)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)

    def test_tool_list_covers_graph_version_and_reads(self):
        names = {spec["name"] for spec in agent.tool_specs()}
        self.assertTrue(
            {
                "add_node",
                "add_edge",
                "remove_node",
                "lock_in",
                "walk_back",
                "branch",
                "merge",
                "switch",
                "inspect_graph",
                "find_orphans",
                "diff_versions",
                "read_file",
                "submit_plan",
                "finish",
            }
            <= names
        )

    def test_plan_act_finish_writes_json_and_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            client = ScriptedClient(
                [
                    ModelTurn(
                        tool_uses=[
                            _use(
                                "p",
                                "submit_plan",
                                {"steps": ["add ignition", "add cooling", "link them"]},
                            )
                        ]
                    ),
                    ModelTurn(
                        tool_uses=[
                            _use(
                                "a",
                                "add_node",
                                {
                                    "id": "ignition",
                                    "label": "ignition",
                                    "kind": "component",
                                    "description": "starts the burn",
                                },
                            ),
                            _use(
                                "b",
                                "add_node",
                                {
                                    "id": "cooling",
                                    "label": "cooling",
                                    "kind": "component",
                                    "description": "takes the heat",
                                },
                            ),
                        ]
                    ),
                    ModelTurn(
                        tool_uses=[
                            _use(
                                "e",
                                "add_edge",
                                {"src": "ignition", "dst": "cooling", "label": "feeds"},
                            )
                        ]
                    ),
                    ModelTurn(
                        tool_uses=[
                            _use(
                                "f",
                                "finish",
                                {
                                    "summary": "Added ignition and cooling and linked them.",
                                    "uncertainties": ["heat path is unspecified"],
                                    "questions": ["Should cooling feed back into ignition?"],
                                },
                            )
                        ]
                    ),
                ]
            )
            report = agent.run_agent(session, client)

            model = session.store.current()
            self.assertIn("ignition", model["nodes"])
            self.assertIn("cooling", model["nodes"])
            self.assertEqual(model["edges"][0]["src"], "ignition")
            self.assertEqual(model["edges"][0]["dst"], "cooling")
            self.assertEqual(session.store.current_version(), "v0.0.3")
            self.assertTrue(report.checks_passed)
            self.assertEqual(report.stop_reason, "finished")
            self.assertEqual(report.steps, 4)
            self.assertEqual(report.plan, ["add ignition", "add cooling", "link them"])
            self.assertIn("ignition", report.summary)
            self.assertEqual(report.uncertainties, ["heat path is unspecified"])
            self.assertEqual(report.questions, ["Should cooling feed back into ignition?"])
            self.assertEqual(exit_code(report), 0)
            self.assertTrue(any(path.endswith("model.json") for path in report.artifacts))
            self.assertTrue((session.paths["artifacts"] / "model.json").is_file())
            # Ideation writes JSON only; the other formats stay previews.
            self.assertFalse((session.paths["artifacts"] / "model.md").exists())
            self.assertIn("submit_plan", client.systems[0])

            turns = [
                json.loads(line)
                for line in session.paths["trace"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(turns[-1]["parsed"]["source"], "agent")
            ops = {op["op"] for op in turns[-1]["parsed"]["model_ops"]}
            self.assertEqual(ops, {"add_node", "add_edge"})
            self.assertTrue(session.paths["state"].is_file())

    def test_finish_is_rejected_until_orphans_are_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["link the two units"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"}),
                            _use("b", "add_node", {"id": "cooling", "label": "cooling", "kind": "component"}),
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f1", "finish", {"summary": "both units exist"})]),
                    ModelTurn(
                        tool_uses=[
                            _use("e", "add_edge", {"src": "ignition", "dst": "cooling", "label": "feeds"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f2", "finish", {"summary": "linked them"})]),
                ]
            )
            report = agent.run_agent(session, client)
            blob = json.dumps(client.seen[3])
            self.assertIn("orphan", blob)
            self.assertIn("Checks still fail", blob)
            self.assertTrue(report.checks_passed)
            self.assertEqual(report.summary, "linked them")
            self.assertEqual(len(session.store.current()["edges"]), 1)

    def test_text_only_stop_is_nudged_while_gaps_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["connect them"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"}),
                            _use("b", "add_node", {"id": "cooling", "label": "cooling", "kind": "component"}),
                        ]
                    ),
                    ModelTurn(text="I think this is done."),
                    ModelTurn(
                        tool_uses=[
                            _use("e", "add_edge", {"src": "ignition", "dst": "cooling", "label": "feeds"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f", "finish", {"summary": "linked after the nudge"})]),
                ]
            )
            report = agent.run_agent(session, client)
            self.assertIn("Checks still fail", json.dumps(client.seen[3]))
            self.assertEqual(report.stop_reason, "finished")
            self.assertTrue(report.checks_passed)

    def test_finish_after_the_fix_in_the_same_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["link them"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"}),
                            _use("b", "add_node", {"id": "cooling", "label": "cooling", "kind": "component"}),
                        ]
                    ),
                    ModelTurn(
                        tool_uses=[
                            _use("f", "finish", {"summary": "linked in one step"}),
                            _use("e", "add_edge", {"src": "ignition", "dst": "cooling", "label": "feeds"}),
                        ]
                    ),
                ]
            )
            report = agent.run_agent(session, client)
            self.assertEqual(report.steps, 3)
            self.assertEqual(report.stop_reason, "finished")
            self.assertTrue(report.checks_passed)

    def test_step_cap_reports_remaining_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            session.max_steps = 2
            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["add both"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"}),
                            _use("b", "add_node", {"id": "cooling", "label": "cooling", "kind": "component"}),
                        ]
                    ),
                ]
            )
            report = agent.run_agent(session, client)
            self.assertEqual(len(client.seen), 2)
            self.assertEqual(report.stop_reason, "step_cap")
            self.assertFalse(report.checks_passed)
            self.assertEqual(report.steps, 2)
            self.assertTrue(any(gap["kind"] == "orphan" for gap in report.gaps))
            self.assertTrue(any("has no relations" in item for item in report.uncertainties))
            self.assertTrue(report.questions)
            self.assertEqual(exit_code(report), 1)
            self.assertIn("step cap", report.summary)

    def test_mutation_before_a_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            client = ScriptedClient(
                [
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["add ignition"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("b", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f", "finish", {"summary": "added ignition"})]),
                ]
            )
            report = agent.run_agent(session, client)
            self.assertIn("submit_plan", json.dumps(client.seen[1]))
            self.assertIn("ignition", session.store.current()["nodes"])
            self.assertEqual(session.store.current_version(), "v0.0.1")
            self.assertTrue(report.checks_passed)

    def test_model_error_stops_and_keeps_partial_work(self):
        class Flaky(ScriptedClient):
            def complete(self, *, system, messages, tools):
                if len(self.seen) >= 2:
                    raise RuntimeError("network down")
                return super().complete(system=system, messages=messages, tools=tools)

        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            client = Flaky(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["add ignition"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
                        ]
                    ),
                ]
            )
            report = agent.run_agent(session, client)
            self.assertEqual(report.stop_reason, "model_error")
            self.assertIn("network down", report.summary)
            self.assertIn("ignition", session.store.current()["nodes"])
            self.assertEqual(exit_code(report), 1)
            self.assertTrue((session.paths["artifacts"] / "model.json").is_file())

    def test_additions_do_not_prompt_but_writes_do_in_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), risk_scope="staging")
            prompts: list[str] = []

            def fake_input(prompt=""):
                prompts.append(prompt)
                if "write " in prompt:
                    return "y"
                raise AssertionError(f"unexpected confirmation: {prompt}")

            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["add ignition"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f", "finish", {"summary": "added ignition"})]),
                ]
            )
            with patch("state.input", side_effect=fake_input):
                report = agent.run_agent(session, client)
            self.assertTrue(prompts)
            self.assertTrue(all("write " in prompt for prompt in prompts))
            self.assertIn("ignition", session.store.current()["nodes"])
            self.assertTrue(report.checks_passed)
            self.assertTrue((session.paths["artifacts"] / "model.json").is_file())

    def test_declined_write_leaves_no_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), risk_scope="staging")

            def fake_input(prompt=""):
                if "write " in prompt:
                    return "n"
                raise AssertionError(prompt)

            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["add ignition"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f", "finish", {"summary": "added ignition"})]),
                ]
            )
            with patch("state.input", side_effect=fake_input):
                report = agent.run_agent(session, client)
            self.assertEqual(report.artifacts, [])
            self.assertFalse((session.paths["artifacts"] / "model.json").exists())
            trace = session.paths["trace"].read_text(encoding="utf-8")
            self.assertIn("declined to write", trace)

    def test_execution_mode_writes_every_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), intent_mode="execution")
            client = ScriptedClient(
                [
                    ModelTurn(tool_uses=[_use("p", "submit_plan", {"steps": ["add ignition"]})]),
                    ModelTurn(
                        tool_uses=[
                            _use("a", "add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
                        ]
                    ),
                    ModelTurn(tool_uses=[_use("f", "finish", {"summary": "added ignition"})]),
                ]
            )
            agent.run_agent(session, client)
            artifacts = session.paths["artifacts"]
            for name in ("model.json", "model.md", "model.mmd", "model_stub.py"):
                self.assertTrue((artifacts / name).is_file(), name)

    def test_confirmation_gates_on_the_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            session.planned = True
            added = session.handle(
                "add_node",
                {"id": "ignition", "label": "ignition", "kind": "component", "description": "burn"},
            )
            self.assertTrue(added["ok"])

            def refuse_input(prompt=""):
                raise AssertionError(f"should not ask in dev: {prompt}")

            with patch("state.input", side_effect=refuse_input):
                removed = session.handle("remove_node", {"id": "ignition"})
            self.assertTrue(removed["ok"])
            self.assertNotIn("ignition", session.store.current()["nodes"])

        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), risk_scope="staging")
            session.planned = True
            session.handle("add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
            prompts: list[str] = []

            def decline(prompt=""):
                prompts.append(prompt)
                return "n"

            with patch("state.input", side_effect=decline):
                result = session.handle("remove_node", {"id": "ignition"})
            self.assertFalse(result["ok"])
            self.assertTrue(result.get("declined"))
            self.assertIn("ignition", session.store.current()["nodes"])
            self.assertTrue(any("remove node" in prompt for prompt in prompts))

            prompts.clear()

            def accept(prompt=""):
                prompts.append(prompt)
                return "y"

            with patch("state.input", side_effect=accept):
                result = session.handle("remove_node", {"id": "ignition"})
            self.assertTrue(result["ok"])
            self.assertNotIn("ignition", session.store.current()["nodes"])

        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), risk_scope="production")
            session.planned = True
            session.handle("add_node", {"id": "ignition", "label": "ignition", "kind": "component"})

            def no_prompt(prompt=""):
                raise AssertionError(f"production should refuse, not ask: {prompt}")

            with patch("state.input", side_effect=no_prompt):
                result = session.handle("remove_node", {"id": "ignition"})
            self.assertFalse(result["ok"])
            self.assertIn("not permitted", result["error"])
            self.assertIn("ignition", session.store.current()["nodes"])

        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), risk_scope="staging", assume_yes=True)
            session.planned = True
            session.handle("add_node", {"id": "ignition", "label": "ignition", "kind": "component"})

            def no_prompt(prompt=""):
                raise AssertionError(prompt)

            with patch("state.input", side_effect=no_prompt):
                result = session.handle("remove_node", {"id": "ignition"})
            self.assertTrue(result["ok"])
            self.assertNotIn("ignition", session.store.current()["nodes"])

    def test_version_bumps_ask_in_staging_and_walk_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp), risk_scope="staging")
            session.planned = True
            prompts: list[str] = []

            def answer(prompt=""):
                prompts.append(prompt)
                return "n" if "lock in" in prompt else "y"

            with patch("state.input", side_effect=answer):
                declined = session.handle("lock_in", {})
            self.assertFalse(declined["ok"])
            self.assertEqual(session.store.current_version(), "v0.0.0")

            prompts.clear()

            def accept(prompt=""):
                prompts.append(prompt)
                return "y"

            with patch("state.input", side_effect=accept):
                locked = session.handle("lock_in", {"note": "baseline"})
            self.assertTrue(locked["ok"])
            self.assertEqual(locked["version"], "v0.1.0")
            info = session.store.get_version("v0.1.0")
            self.assertTrue(info.milestone)
            self.assertTrue(any("lock in" in prompt for prompt in prompts))

            session.handle(
                "add_node",
                {"id": "ignition", "label": "ignition", "kind": "component", "description": "burn"},
            )
            session.handle(
                "add_node",
                {"id": "cooling", "label": "cooling", "kind": "component", "description": "heat"},
            )
            # Two unconnected nodes. Walk back to the milestone, which has neither.
            with patch("state.input", side_effect=accept):
                walked = session.handle("walk_back", {"version": "v0.1.0"})
            self.assertTrue(walked["ok"])
            self.assertEqual(session.store.current_version(), "v0.1.0")
            self.assertEqual(session.store.current()["nodes"], {})

            with patch("state.input", return_value="n"):
                branched = session.handle("branch", {"name": "cooling"})
            self.assertFalse(branched["ok"])
            self.assertEqual(session.store.branches(), ["main"])

    def test_dev_lock_in_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            session.planned = True

            def refuse_input(prompt=""):
                raise AssertionError(prompt)

            with patch("state.input", side_effect=refuse_input):
                locked = session.handle("lock_in", {})
            self.assertTrue(locked["ok"])
            self.assertEqual(locked["version"], "v0.1.0")

    def test_branch_switch_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            session.planned = True
            created = session.handle("branch", {"name": "cooling"})
            self.assertTrue(created["ok"])
            self.assertEqual(session.store.current_branch(), "cooling")
            session.handle(
                "add_node",
                {"id": "radiator", "label": "radiator", "kind": "component", "description": "dumps heat"},
            )
            switched = session.handle("switch", {"name": "main"})
            self.assertTrue(switched["ok"])
            self.assertNotIn("radiator", session.store.current()["nodes"])
            merged = session.handle("merge", {"name": "cooling"})
            self.assertTrue(merged["ok"])
            self.assertIn("radiator", session.store.current()["nodes"])

    def test_edge_implies_nodes_until_they_are_described(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            session.planned = True
            added = session.handle("add_edge", {"src": "ignition", "dst": "cooling", "label": "feeds"})
            self.assertTrue(added["ok"])
            self.assertIn("incomplete", _kinds(session.store.current()))
            session.handle(
                "update_node",
                {
                    "id": "ignition",
                    "label": "ignition",
                    "kind": "component",
                    "description": "starts the burn",
                },
            )
            session.handle(
                "update_node",
                {
                    "id": "cooling",
                    "label": "cooling",
                    "kind": "component",
                    "description": "takes the heat",
                },
            )
            self.assertEqual(audit_graph(session.store.current()), [])

    def test_attributes_notes_metadata_and_remove_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            session.planned = True
            session.handle("add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
            session.handle("add_node", {"id": "cooling", "label": "cooling", "kind": "component"})
            session.handle("add_edge", {"src": "ignition", "dst": "cooling", "label": "feeds"})
            self.assertTrue(
                session.handle("set_attr", {"id": "ignition", "key": "temp", "value": "high"})["ok"]
            )
            self.assertEqual(session.store.current()["nodes"]["ignition"]["attrs"]["temp"], "high")
            self.assertTrue(session.handle("add_tag", {"id": "ignition", "value": "hot"})["ok"])
            self.assertIn("hot", session.store.current()["nodes"]["ignition"]["tags"])
            self.assertTrue(session.handle("add_note", {"text": "check the gasket"})["ok"])
            self.assertIn("check the gasket", session.store.current()["notes"])
            self.assertTrue(session.handle("set_meta", {"key": "name", "value": "Powertrain"})["ok"])
            self.assertEqual(session.store.current()["meta"]["name"], "Powertrain")

            def refuse_input(prompt=""):
                raise AssertionError(prompt)

            with patch("state.input", side_effect=refuse_input):
                removed = session.handle("remove_edge", {"src": "ignition", "dst": "cooling"})
            self.assertTrue(removed["ok"])
            self.assertEqual(session.store.current()["edges"], [])

    def test_read_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = _session(root)
            session.planned = True
            session.handle("add_node", {"id": "ignition", "label": "ignition", "kind": "component"})
            session.handle("add_node", {"id": "cooling", "label": "cooling", "kind": "component"})
            orphans = session.handle("find_orphans", {})
            self.assertEqual(len(orphans["orphans"]), 2)
            inspected = session.handle("inspect_graph", {})
            self.assertEqual(set(inspected["nodes"]), {"ignition", "cooling"})
            self.assertFalse(inspected["checks"]["passed"])
            diff = session.handle("diff_versions", {"a": "v0.0.1", "b": "v0.0.2"})
            self.assertTrue(diff["ok"])
            self.assertTrue(diff["changes"])
            missing = session.handle("diff_versions", {"a": "v0.0.1", "b": "v9.9.9"})
            self.assertFalse(missing["ok"])

            docs = root / "docs"
            docs.mkdir()
            (docs / "brief.md").write_text("The pump is called primer.\n", encoding="utf-8")
            session.read_roots = [session.paths["workspace"], docs]
            found = session.handle("read_file", {"path": "brief.md"})
            self.assertTrue(found["ok"])
            self.assertIn("primer", found["text"])

            secret = root / "secret.txt"
            secret.write_text("nope", encoding="utf-8")
            escaped = session.handle("read_file", {"path": "../secret.txt"})
            self.assertFalse(escaped["ok"])
            self.assertIn("outside", escaped["error"])
            absolute = session.handle("read_file", {"path": str(secret)})
            self.assertFalse(absolute["ok"])

            outside = root / "outside.txt"
            outside.write_text("hidden", encoding="utf-8")
            link = session.paths["workspace"] / "link.txt"
            link.symlink_to(outside)
            session.read_roots = [session.paths["workspace"]]
            followed = session.handle("read_file", {"path": "link.txt"})
            self.assertFalse(followed["ok"])
            self.assertIn("outside", followed["error"])

            (session.paths["workspace"] / ".env").write_text("ANTHROPIC_API_KEY=secret\n", encoding="utf-8")
            env_file = session.handle("read_file", {"path": ".env"})
            self.assertFalse(env_file["ok"])
            self.assertIn("sensitive", env_file["error"])

            (session.paths["workspace"] / "blob.bin").write_bytes(b"abc\x00def")
            binary = session.handle("read_file", {"path": "blob.bin"})
            self.assertFalse(binary["ok"])
            self.assertIn("binary", binary["error"])

            long = session.paths["workspace"] / "notes.md"
            long.write_text("abcdefghij" * 10, encoding="utf-8")
            with patch.object(config, "AGENT_READ_MAX_BYTES", 20):
                truncated = session.handle("read_file", {"path": "notes.md"})
            self.assertTrue(truncated["ok"])
            self.assertTrue(truncated["truncated"])
            self.assertEqual(len(truncated["text"]), 20)


if __name__ == "__main__":
    unittest.main()
