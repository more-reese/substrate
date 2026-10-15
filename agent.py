"""Agent mode: a plan → act → check loop that builds the graph toward a goal.

The REPL parses one sentence at a time. Agent mode takes a goal and drives
Claude with tools until the graph is consistent or a step cap is hit. The
tools are the operations the rest of the app already knows how to apply
(`ops_to_writes`, version-store methods, artifact writing). The checks are
ordinary code, so a gap is a fact about the graph and not another opinion
from the model.

Confirmation reuses `SessionState.confirm` and `CONFIRM_POLICY`:

  * routine additions proceed in every risk scope
  * deletions, version-graph changes, and artifact writes ask when the
    current scope lists that kind (dev lists none; staging and production
    list all three)
  * production still refuses deletions outright
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
import generators
import parser as parser_mod
import trace
import version_store as vs
from state import SessionState

# Styling only when a terminal is watching, matching substrate.py.
TTY = sys.stdout.isatty()
DIM, BOLD, RESET = ("\033[2m", "\033[1m", "\033[0m") if TTY else ("", "", "")

# Placeholder written by version_store.ops_to_writes when an edge names a node
# that has not been described yet.
_IMPLIED_DESCRIPTION = "implied by an edge before it was described"

_RELATION_RE = re.compile(
    r"\b(?:feeds?(?:\s+into)?|connect(?:s|ed)?\s+to|depends?\s+on|talks?\s+to|"
    r"calls|sends?\s+to|links?\s+to|flows?\s+into|requires|uses|writes?\s+to|"
    r"reads?\s+from|drives|powers|cools)\b",
    re.I,
)
_SECRET_NAMES = {"id_rsa", "credentials.json", "credentials", "secrets.json"}
_SECRET_SUFFIXES = {".pem", ".key"}

GRAPH_TOOLS = frozenset(
    {
        "add_node",
        "update_node",
        "remove_node",
        "add_edge",
        "remove_edge",
        "set_attr",
        "add_tag",
        "add_note",
        "set_meta",
    }
)
VERSION_TOOLS = frozenset({"lock_in", "walk_back", "branch", "merge", "switch"})
DELETE_TOOLS = frozenset({"remove_node", "remove_edge"})
# Tools that change the graph or which version is current. Each one is checked.
ACTION_TOOLS = GRAPH_TOOLS | VERSION_TOOLS


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


def bold(text: str) -> str:
    return f"{BOLD}{text}{RESET}"


# ---------------------------------------------------------------------------
# Graph checks
# ---------------------------------------------------------------------------


def audit_graph(model: dict | None) -> list[dict]:
    """Find gaps the agent should close before it finishes.

    Returns a list of {"kind", "detail"} dicts. Kinds:

      orphan         a node with no edges, once two or more nodes exist
      missing_edge   an edge names a missing node, or a description claims
                     a relation that the edge list does not have
      contradiction  shared labels, a self-edge, or opposing edge labels
      incomplete     a node that an edge implied and nobody described

    An empty graph, and a single node with no edges, have no gaps: there is
    nothing to connect yet. The caller decides what to do with the list.
    """
    model = model or {}
    nodes = model.get("nodes") if isinstance(model.get("nodes"), dict) else {}
    edges = list(model.get("edges") or [])
    gaps: list[dict] = []

    degree: dict[str, int] = {nid: 0 for nid in nodes}
    labels_by_fold: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        node = node if isinstance(node, dict) else {}
        label = str(node.get("label") or "").strip()
        if label:
            labels_by_fold.setdefault(label.casefold(), []).append(nid)
        if node.get("kind") == "implied" or node.get("description") == _IMPLIED_DESCRIPTION:
            gaps.append(
                {
                    "kind": "incomplete",
                    "detail": (
                        f"node {nid} ({label or nid}) was implied by an edge "
                        "and never described"
                    ),
                }
            )

    for label, ids in sorted(labels_by_fold.items()):
        if len(ids) > 1:
            named = " and ".join(sorted(ids))
            gaps.append(
                {
                    "kind": "contradiction",
                    "detail": f"nodes {named} share the label {label!r}",
                }
            )

    seen_pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        label = str(edge.get("label") or "")
        if not src or not dst or src not in nodes or dst not in nodes:
            gaps.append(
                {
                    "kind": "missing_edge",
                    "detail": (
                        f"edge {src or '∅'} → {dst or '∅'}"
                        + (f" ({label})" if label else "")
                        + " names a node that is not in the graph"
                    ),
                }
            )
            continue
        if src == dst:
            gaps.append(
                {
                    "kind": "contradiction",
                    "detail": f"edge {src} → {dst} relates a node to itself",
                }
            )
        degree[src] = degree.get(src, 0) + 1
        if dst != src:
            degree[dst] = degree.get(dst, 0) + 1

    # Opposing labels on the same directed pair: "feeds" against "not feeds".
    grouped: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        if src and dst and src in nodes and dst in nodes and src != dst:
            grouped.setdefault((src, dst), []).append(str(edge.get("label") or ""))
    for (src, dst), labels in sorted(grouped.items()):
        parsed = [(_negation(label), label) for label in labels]
        for index, (left, raw_left) in enumerate(parsed):
            for right, raw_right in parsed[index + 1 :]:
                if left[1] and left[1] == right[1] and left[0] != right[0]:
                    gaps.append(
                        {
                            "kind": "contradiction",
                            "detail": (
                                f"edges {src} → {dst} contradict: "
                                f"{raw_left!r} vs {raw_right!r}"
                            ),
                        }
                    )

    if len(nodes) >= 2:
        for nid in sorted(degree):
            if degree[nid] == 0:
                label = str((nodes.get(nid) or {}).get("label") or nid)
                gaps.append(
                    {
                        "kind": "orphan",
                        "detail": f"node {nid} ({label}) has no relations",
                    }
                )

    described = []
    for nid, node in sorted(nodes.items()):
        node = node if isinstance(node, dict) else {}
        described.append((nid, str(node.get("label") or ""), str(node.get("description") or "")))
    for nid, _label, description in described:
        if not description or not _RELATION_RE.search(description):
            continue
        for other, other_label, _other_description in described:
            if other == nid:
                continue
            pair = tuple(sorted((nid, other)))
            if pair in seen_pairs:
                continue
            if not (
                _mentions(description, other_label)
                or _mentions(description, other.replace("_", " "))
            ):
                continue
            if _linked(edges, nid, other):
                seen_pairs.add(pair)
                continue
            seen_pairs.add(pair)
            gaps.append(
                {
                    "kind": "missing_edge",
                    "detail": (
                        f"no edge between {nid} and {other}, "
                        f"but {nid} describes a relation"
                    ),
                }
            )

    gaps.sort(key=lambda gap: (gap["kind"], gap["detail"]))
    return gaps


def _negation(label: str) -> tuple[bool, str]:
    """Return (is_negated, stem) so 'not feeds' lines up with 'feeds'."""
    text = re.sub(r"\s+", " ", (label or "").strip().lower())
    negated = False
    for prefix in ("does not ", "doesn't ", "do not ", "don't ", "not ", "never "):
        if text.startswith(prefix):
            negated = True
            text = text[len(prefix) :].strip()
            break
    if len(text) > 3 and text.endswith("s"):
        text = text[:-1]
    return negated, text


def _mentions(text: str, phrase: str) -> bool:
    phrase = (phrase or "").strip()
    if len(phrase) < 4:
        return False
    escaped = re.escape(phrase)
    if not re.search(rf"\b{escaped}\b", text, re.I):
        return False
    # A nearby negation ("not connected to cooling") is not a claim that the
    # edge exists. Quantifiers are written with doubled braces because this
    # pattern is an f-string.
    negated = re.compile(
        rf"\b(?:not|never|no|without|doesn't|does\s+not|don't|do\s+not)\b"
        rf"(?:\W+\w+){{0,6}}\W+{escaped}\b",
        re.I,
    )
    if negated.search(text):
        return False
    return True


def _linked(edges: list, left: str, right: str) -> bool:
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        ends = {str(edge.get("src") or ""), str(edge.get("dst") or "")}
        if ends == {left, right}:
            return True
    return False


def checks_payload(model: dict | None) -> dict:
    gaps = audit_graph(model)
    return {"passed": not gaps, "gaps": gaps}


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------


@dataclass
class ToolUse:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ModelTurn:
    """One model response. `assistant_content` is sent back verbatim when set,
    so a real API response (including any extra blocks) round-trips."""

    text: str = ""
    tool_uses: list[ToolUse] = field(default_factory=list)
    assistant_content: list | None = None


class AnthropicToolClient:
    """Claude tool use via the Messages API. Tests never construct this."""

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def complete(self, *, system: str, messages: list, tools: list) -> ModelTurn:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=config.MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=tools,
            messages=messages,
        )
        blocks = [_plain_block(block) for block in response.content]
        text = "\n".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        uses = []
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            arguments = block.get("input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            uses.append(
                ToolUse(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                )
            )
        return ModelTurn(text=text, tool_uses=uses, assistant_content=blocks)


def _plain_block(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        data = block.model_dump(exclude_none=True)
        if isinstance(data, dict):
            return data
    kind = getattr(block, "type", "")
    if kind == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input if isinstance(block.input, dict) else {},
        }
    return {"type": str(kind or "text"), "text": str(block)}


def make_agent_client(model: str) -> tuple[AnthropicToolClient | None, str | None]:
    """Return (client, warning). Warning is set when Claude cannot be called."""
    try:
        import anthropic
    except ImportError:
        return None, (
            "the `anthropic` package is not installed, so agent mode cannot call "
            "Claude. Install requirements.txt. The REPL still runs with --dry-run."
        )
    try:
        client = anthropic.Anthropic(
            timeout=config.AGENT_REQUEST_TIMEOUT, max_retries=config.MAX_RETRIES
        )
    except Exception as exc:  # noqa: BLE001 — startup should report, not crash
        return None, (
            f"could not start the Anthropic client ({type(exc).__name__}: {exc})."
        )
    if not parser_mod._credentials_available(client):
        return None, (
            "ANTHROPIC_API_KEY is not set (and no `ant auth login` profile was "
            "found). Agent mode needs Claude tool use. The REPL still runs "
            "offline with --dry-run."
        )
    return AnthropicToolClient(client, model), None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are the agent inside `substrate`. The user gives you one goal. You turn it
into a versioned system graph by calling tools. Do not answer with prose alone.

The graph holds nodes (id, label, kind, description), edges (src, dst, label),
notes, and metadata. Node ids are lowercase_snake_case. Reuse an existing id
when you mean a node that is already there. Prefer update_node over a second
add_node for something that already exists. add_edge src and dst are node ids.

Loop, in order:
  1. Look. Call inspect_graph. If the goal depends on a document, call
     read_file. It can only read the workspace and the docs directories named
     in the goal message.
  2. Plan. Call submit_plan with concrete steps before any change. Changes
     are rejected until you do.
  3. Act. Carry the plan out with graph and version tools.
  4. Read the checks on every change. Fix every gap before finish.
  5. Finish. Call finish only when checks.passed is true.

Checks are computed by code:
  orphan — two or more nodes exist and this one has no edges
  missing_edge — an edge names a missing node, or a description says one node
    feeds, connects to, depends on, or otherwise relates to another and no
    edge runs between them
  contradiction — two nodes share a label, an edge links a node to itself, or
    the same pair has opposing labels (feeds against not feeds)
  incomplete — a node exists only because an edge implied it and it was never
    given a real kind and description (update_node fixes this)

Guardrails come from the session's confirmation settings:
  Routine additions (add_node, update_node, add_edge, set_attr, add_tag,
  add_note, set_meta) proceed without asking, in every risk scope.
  Deletions (remove_node, remove_edge), version changes (lock_in, walk_back,
  branch, merge), and writing artifact files ask when the risk scope says so.
  dev asks for none of them. staging and production ask. production refuses
  deletions outright. switch moves between branches and does not ask.
  A declined or refused call is final — change the plan, do not retry it.
  Ordinary additions already commit a patch version. Call lock_in only when
  the goal asks for a milestone; that bumps the minor version.
  Artifact files are written for you after you finish. There is no write tool.

finish.summary says what changed. finish.uncertainties are things you could
not honestly close. finish.questions are decisions only the user can make.
Use an empty list when you have none. Do not invent questions.

When a tool returns ok: false, believe it and adapt.
"""


def tool_specs() -> list[dict]:
    """JSON schemas handed to Claude. Handlers live on AgentSession."""

    def spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    string = {"type": "string"}
    string_list = {"type": "array", "items": {"type": "string"}}
    return [
        spec(
            "inspect_graph",
            "Read the current graph, version, branch, and the latest checks. "
            "Call this before planning.",
            {},
            [],
        ),
        spec(
            "find_orphans",
            "List nodes that have no edges. Empty when the graph is connected "
            "or has fewer than two nodes.",
            {},
            [],
        ),
        spec(
            "diff_versions",
            "Compare two committed versions. Version ids look like v0.0.1.",
            {"a": string, "b": string},
            ["a", "b"],
        ),
        spec(
            "read_file",
            "Read a text file from the workspace or a docs directory. "
            "Relative paths are resolved against those roots. Paths outside "
            "them, and secret files, are refused.",
            {"path": string},
            ["path"],
        ),
        spec(
            "submit_plan",
            "Record the plan before changing the graph. steps is a list of "
            "short actions. uncertainties are optional things you already "
            "know you cannot decide.",
            {"steps": string_list, "uncertainties": string_list},
            ["steps"],
        ),
        spec(
            "add_node",
            "Add a node, or fill in a node that already has this id. Proceeds "
            "without asking. id is lowercase_snake_case; label is the name "
            "a person would say.",
            {
                "id": string,
                "label": string,
                "kind": string,
                "description": string,
            },
            ["label"],
        ),
        spec(
            "update_node",
            "Change the label, kind, or description of an existing node. "
            "Use this to replace a node that an edge only implied.",
            {
                "id": string,
                "label": string,
                "kind": string,
                "description": string,
            },
            ["id"],
        ),
        spec(
            "remove_node",
            "Delete a node and its edges. Asks in staging and production; "
            "production refuses. A decline is final.",
            {"id": string, "label": string},
            ["id"],
        ),
        spec(
            "add_edge",
            "Link two existing node ids. Proceeds without asking. If an "
            "endpoint does not exist yet it is created as an implied node, "
            "which then fails the incomplete check until you describe it.",
            {"src": string, "dst": string, "label": string},
            ["src", "dst"],
        ),
        spec(
            "remove_edge",
            "Delete the edge from src to dst. Asks in staging and production; "
            "production refuses.",
            {"src": string, "dst": string},
            ["src", "dst"],
        ),
        spec(
            "set_attr",
            "Set a string attribute on a node. Proceeds without asking.",
            {"id": string, "key": string, "value": string},
            ["id", "key", "value"],
        ),
        spec(
            "add_tag",
            "Add a tag to a node. Proceeds without asking.",
            {"id": string, "value": string},
            ["id", "value"],
        ),
        spec(
            "add_note",
            "Record a note that does not belong in the graph structure. "
            "Proceeds without asking.",
            {"text": string},
            ["text"],
        ),
        spec(
            "set_meta",
            "Set model metadata. key is usually name or summary. Proceeds "
            "without asking.",
            {"key": string, "value": string},
            ["key", "value"],
        ),
        spec(
            "lock_in",
            "Seal a milestone. Bumps the minor version. Asks in staging and "
            "production. Ordinary edits already commit a patch version.",
            {"note": string},
            [],
        ),
        spec(
            "walk_back",
            "Make an earlier version the base for what comes next. Asks in "
            "staging and production.",
            {"version": string},
            ["version"],
        ),
        spec(
            "branch",
            "Fork the version graph and switch to the fork. Asks in staging "
            "and production.",
            {"name": string},
            ["name"],
        ),
        spec(
            "merge",
            "Merge another branch into the current one. Asks in staging and "
            "production.",
            {"name": string},
            ["name"],
        ),
        spec(
            "switch",
            "Move to another branch. Does not ask.",
            {"name": string},
            ["name"],
        ),
        spec(
            "finish",
            "Stop. Only succeeds when a plan was submitted and the checks "
            "pass. summary is what changed. uncertainties and questions may "
            "be empty lists.",
            {
                "summary": string,
                "uncertainties": string_list,
                "questions": string_list,
            },
            ["summary"],
        ),
    ]


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass
class AgentReport:
    goal: str
    plan: list[str]
    summary: str
    uncertainties: list[str]
    questions: list[str]
    changes: list[str]
    gaps: list[dict]
    checks_passed: bool
    steps: int
    stop_reason: str
    versions: list[str]
    artifacts: list[str] = field(default_factory=list)


class AgentSession:
    """Tool handlers bound to one workspace. The loop lives in `run_agent`."""

    def __init__(
        self,
        store,
        state: SessionState,
        paths: dict,
        goal: str,
        read_roots: list[Path],
        lexicon_text: str = "(empty — no terms have been coined yet)",
        max_steps: int = config.AGENT_MAX_STEPS,
    ):
        self.store = store
        self.state = state
        self.paths = paths
        self.goal = goal.strip()
        self.read_roots = [Path(root) for root in read_roots]
        self.lexicon_text = lexicon_text or "(empty — no terms have been coined yet)"
        self.max_steps = max_steps

        self.plan: list[str] = []
        self.plan_uncertainties: list[str] = []
        self.planned = False
        self.finished = False
        self.summary = ""
        self.uncertainties: list[str] = []
        self.questions: list[str] = []
        self.closing_text = ""
        self.model_error = ""
        self.changes: list[str] = []
        self.versions: list[str] = []
        self.applied_ops: list[dict] = []
        self.effects: list[str] = []

    # -- prompt -------------------------------------------------------------

    def opening_message(self) -> str:
        roots = "\n".join(f"  - {path}" for path in self._resolved_roots()) or "  (none)"
        return f"""\
GOAL
{self.goal}

CURRENT STATE
  intent_mode:    {self.state.intent_mode}
  risk_scope:     {self.state.risk_scope}
  focus:          {self.state.focus_key}
  version:        {self.store.current_version()}
  branch:         {self.store.current_branch()}

DOCUMENTS read_file MAY READ
{roots}

LEXICON
{self.lexicon_text}

CURRENT MODEL
{parser_mod.model_summary(self.store.current())}

Begin with inspect_graph, then submit_plan, then act.
"""

    def check_payload(self) -> dict:
        return checks_payload(self.store.current())

    # -- dispatch -----------------------------------------------------------

    def handle(self, name: str, arguments: dict | None) -> dict:
        arguments = arguments if isinstance(arguments, dict) else {}
        if name in ACTION_TOOLS and not self.planned:
            return {
                "ok": False,
                "error": "Submit a plan with submit_plan before changing the graph.",
            }
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        return handler(arguments)

    # -- read tools ---------------------------------------------------------

    def _tool_inspect_graph(self, _args: dict) -> dict:
        model = self.store.current()
        version = self.store.current_version()
        print(dim(f"  · inspect {version} on {self.store.current_branch()}"))
        return {
            "ok": True,
            "version": version,
            "branch": self.store.current_branch(),
            "focus": self.state.focus_key,
            "intent_mode": self.state.intent_mode,
            "risk_scope": self.state.risk_scope,
            "meta": model.get("meta", {}),
            "nodes": model.get("nodes", {}),
            "edges": model.get("edges", []),
            "notes": model.get("notes", []),
            "checks": self.check_payload(),
        }

    def _tool_find_orphans(self, _args: dict) -> dict:
        orphans = [gap for gap in audit_graph(self.store.current()) if gap["kind"] == "orphan"]
        print(dim(f"  · orphans: {len(orphans)}"))
        return {"ok": True, "orphans": orphans}

    def _tool_diff_versions(self, args: dict) -> dict:
        left, right = _text(args.get("a")), _text(args.get("b"))
        if not left or not right:
            return {"ok": False, "error": "diff_versions needs two version ids"}
        try:
            result = self.store.diff(left, right)
        except vs.VersionNotFound as exc:
            return {"ok": False, "error": f"no such version: {exc.args[0]}"}
        changes = result.get("changes") or []
        print(dim(f"  · diff {result.get('a')} {result.get('b')} ({len(changes)} change(s))"))
        return {
            "ok": True,
            "a": result.get("a"),
            "b": result.get("b"),
            "changes": changes,
            "detail": result.get("detail"),
        }

    def _tool_read_file(self, args: dict) -> dict:
        raw = _text(args.get("path"))
        print(dim(f"  · read {raw or '(no path)'}"))
        try:
            found = self._resolve_doc(raw)
        except (PermissionError, FileNotFoundError, ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        try:
            blob, truncated = _read_capped(found, config.AGENT_READ_MAX_BYTES)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if b"\x00" in blob:
            return {"ok": False, "error": f"refusing to read binary file: {found.name}"}
        return {
            "ok": True,
            "path": str(found),
            "text": blob.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }

    def _resolved_roots(self) -> list[Path]:
        roots: list[Path] = []
        for root in self.read_roots:
            resolved = Path(root).resolve()
            if resolved not in roots:
                roots.append(resolved)
        return roots

    def _resolve_doc(self, raw: str) -> Path:
        if not raw:
            raise ValueError("read_file needs a path")
        candidate = Path(raw)
        roots = self._resolved_roots()
        options = [candidate] if candidate.is_absolute() else [root / candidate for root in roots]
        saw_outside = False
        saw_inside = False
        for option in options:
            resolved = option.resolve()
            inside = any(resolved == root or root in resolved.parents for root in roots)
            if not inside:
                saw_outside = True
                continue
            saw_inside = True
            if _is_secret(resolved):
                raise PermissionError(f"refusing to read sensitive file: {resolved.name}")
            if resolved.is_file():
                return resolved
        if saw_outside and not saw_inside:
            raise PermissionError(f"refusing to read outside allowed docs: {raw}")
        raise FileNotFoundError(f"no such file: {raw}")

    # -- plan and finish ----------------------------------------------------

    def _tool_submit_plan(self, args: dict) -> dict:
        steps = _str_list(args.get("steps"))
        if not steps:
            return {"ok": False, "error": "submit_plan needs at least one step"}
        self.plan = steps
        self.plan_uncertainties = _str_list(args.get("uncertainties"))
        self.planned = True
        self.effects.append("plan: " + " | ".join(steps))
        print("  plan:")
        for index, step in enumerate(steps, start=1):
            print(f"    {index}. {step}")
        return {"ok": True, "steps": steps}

    def _tool_finish(self, args: dict) -> dict:
        payload = self.check_payload()
        if not self.planned:
            return {
                "ok": False,
                "error": "Submit a plan with submit_plan before finishing.",
                "checks": payload,
            }
        summary = _text(args.get("summary"))
        if not summary:
            return {"ok": False, "error": "finish needs a summary of what changed", "checks": payload}
        if not payload["passed"]:
            return {
                "ok": False,
                "error": "Checks still fail. Fix the gaps, then call finish again.",
                "checks": payload,
            }
        self.summary = summary
        self.uncertainties = _str_list(args.get("uncertainties"))
        self.questions = _str_list(args.get("questions"))
        self.finished = True
        self.effects.append(f"finish: {summary}")
        return {"ok": True, "checks": payload}

    # -- graph tools --------------------------------------------------------

    def _tool_add_node(self, args: dict) -> dict:
        label = _text(args.get("label"))
        nid = _text(args.get("id"))
        if not label and not nid:
            return {"ok": False, "error": "add_node needs a label or an id"}
        return self._apply_graph_op(
            {
                "op": "add_node",
                "id": nid,
                "label": label or nid,
                "kind": _text(args.get("kind")),
                "description": _text(args.get("description")),
            }
        )

    def _tool_update_node(self, args: dict) -> dict:
        nid = _text(args.get("id")) or _text(args.get("label"))
        if not nid:
            return {"ok": False, "error": "update_node needs an id"}
        return self._apply_graph_op(
            {
                "op": "update_node",
                "id": nid,
                "label": _text(args.get("label")),
                "kind": _text(args.get("kind")),
                "description": _text(args.get("description")),
            }
        )

    def _tool_remove_node(self, args: dict) -> dict:
        nid = _text(args.get("id")) or _text(args.get("label"))
        if not nid:
            return {"ok": False, "error": "remove_node needs an id"}
        return self._apply_graph_op({"op": "remove_node", "id": nid, "label": _text(args.get("label"))})

    def _tool_add_edge(self, args: dict) -> dict:
        src, dst = _text(args.get("src")), _text(args.get("dst"))
        if not src or not dst:
            return {"ok": False, "error": "add_edge needs src and dst"}
        return self._apply_graph_op(
            {"op": "add_edge", "src": src, "dst": dst, "label": _text(args.get("label"))}
        )

    def _tool_remove_edge(self, args: dict) -> dict:
        src, dst = _text(args.get("src")), _text(args.get("dst"))
        if not src or not dst:
            return {"ok": False, "error": "remove_edge needs src and dst"}
        return self._apply_graph_op({"op": "remove_edge", "src": src, "dst": dst})

    def _tool_set_attr(self, args: dict) -> dict:
        nid, key = _text(args.get("id")), _text(args.get("key"))
        if not nid or not key:
            return {"ok": False, "error": "set_attr needs an id and a key"}
        return self._apply_graph_op(
            {"op": "set_attr", "id": nid, "key": key, "value": _text(args.get("value"))}
        )

    def _tool_add_tag(self, args: dict) -> dict:
        nid, tag = _text(args.get("id")), _text(args.get("value"))
        if not nid or not tag:
            return {"ok": False, "error": "add_tag needs an id and a value"}
        return self._apply_graph_op({"op": "add_tag", "id": nid, "value": tag})

    def _tool_add_note(self, args: dict) -> dict:
        text = _text(args.get("text"))
        if not text:
            return {"ok": False, "error": "add_note needs text"}
        return self._apply_graph_op({"op": "add_note", "text": text})

    def _tool_set_meta(self, args: dict) -> dict:
        key, value = _text(args.get("key")), _text(args.get("value"))
        if not key or not value:
            return {"ok": False, "error": "set_meta needs a key and a value"}
        return self._apply_graph_op({"op": "set_meta", "key": key, "value": value})

    def _apply_graph_op(self, op: dict) -> dict:
        kind = op["op"]
        preview = vs.ops_to_writes(
            self.store.current(),
            [op],
            focus_key=self.state.focus_key,
            allow_delete=not self.state.deletes_forbidden(),
        )
        if not preview.writes:
            if preview.refusals:
                message = "; ".join(preview.refusals)
                self.effects.append(f"refused: {message}")
                print(f"  ! {message}")
                return {
                    "ok": False,
                    "error": message,
                    "refusals": preview.refusals,
                    "version": self.store.current_version(),
                }
            return {
                "ok": True,
                "summaries": preview.summaries or ["no change"],
                "version": self.store.current_version(),
            }

        if kind in DELETE_TOOLS:
            description = _delete_description(op)
            if not self.state.confirm(description, "delete"):
                self.effects.append(f"declined {description}")
                print(f"  declined: {description}")
                return {
                    "ok": False,
                    "declined": True,
                    "error": "declined by the confirmation gate",
                }

        self.store.update(preview.writes)
        headline = preview.summaries[0] if preview.summaries else kind
        info = self._commit(f"agent: {headline}")
        self._note_change(preview.summaries, info.id, op)
        for summary in preview.summaries:
            print(f"  + {summary}")
        print(f"  ▸ {bold(info.id)}")
        return {
            "ok": True,
            "summaries": preview.summaries,
            "refusals": preview.refusals,
            "version": info.id,
        }

    # -- version tools ------------------------------------------------------

    def _tool_lock_in(self, args: dict) -> dict:
        description = f"lock in a milestone at {self.store.current_version()}"
        if not self.state.confirm(description, "structural"):
            self.effects.append("declined lock_in")
            print("  declined: lock in")
            return {"ok": False, "declined": True, "error": "declined by the confirmation gate"}
        note = _text(args.get("note"))
        info = self._commit(
            f"agent: lock in{(': ' + note) if note else ''}",
            milestone=True,
            notes=note or None,
        )
        summary = f"locked in {info.id}"
        self._note_change([summary], info.id, None)
        self.effects.append(f"milestone {info.id}")
        print(f"  ★ locked in {bold(info.id)} (milestone)")
        return {"ok": True, "version": info.id, "milestone": True, "summaries": [summary]}

    def _tool_walk_back(self, args: dict) -> dict:
        version = _text(args.get("version") or args.get("version_id"))
        if not version:
            return {"ok": False, "error": "walk_back needs a version"}
        description = f"walk back to {version}"
        if not self.state.confirm(description, "structural"):
            self.effects.append("declined walk_back")
            print(f"  declined: {description}")
            return {"ok": False, "declined": True, "error": "declined by the confirmation gate"}
        try:
            info = self.store.checkout(version)
        except vs.VersionNotFound:
            return {"ok": False, "error": f"no such version: {version}"}
        summary = f"walked back to {info.id}"
        self.changes.append(summary)
        self.effects.append(summary)
        print(f"  ⟲ {summary}")
        return {"ok": True, "version": info.id, "summaries": [summary]}

    def _tool_branch(self, args: dict) -> dict:
        name = _text(args.get("name")) or "variant"
        description = f"branch off {self.store.current_version()} as {name}"
        if not self.state.confirm(description, "structural"):
            self.effects.append("declined branch")
            print(f"  declined: {description}")
            return {"ok": False, "declined": True, "error": "declined by the confirmation gate"}
        try:
            created = self.store.branch(name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        summary = f"branched → {created}"
        self.changes.append(summary)
        self.effects.append(summary)
        print(f"  ⑂ {summary}")
        return {"ok": True, "branch": created, "summaries": [summary]}

    def _tool_merge(self, args: dict) -> dict:
        name = _text(args.get("name"))
        if not name:
            return {"ok": False, "error": "merge needs a branch name"}
        description = f"merge {name} into {self.store.current_branch()}"
        if not self.state.confirm(description, "structural"):
            self.effects.append("declined merge")
            print(f"  declined: {description}")
            return {"ok": False, "declined": True, "error": "declined by the confirmation gate"}
        try:
            changes = self.store.merge(name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        summary = f"merged {name} ({len(changes)} change(s))"
        self.changes.append(summary)
        self.effects.append(summary)
        print(f"  ⑃ {summary}")
        return {"ok": True, "summaries": [summary], "changes": changes}

    def _tool_switch(self, args: dict) -> dict:
        name = _text(args.get("name"))
        if not name:
            return {"ok": False, "error": "switch needs a branch name"}
        try:
            current = self.store.switch(name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        version = self.store.current_version()
        summary = f"switched to {current} at {version}"
        self.changes.append(summary)
        self.effects.append(summary)
        print(f"  ⇄ {summary}")
        return {"ok": True, "branch": current, "version": version, "summaries": [summary]}

    # -- shared mutation helpers --------------------------------------------

    def _commit(self, raw: str, milestone: bool = False, notes: str | None = None):
        return self.store.commit(
            raw=raw[:500],
            notes=(notes if notes is not None else self.goal)[:500],
            mode=self.state.intent_mode,
            scope=self.state.risk_scope,
            focus=self.state.focus_key,
            milestone=milestone,
        )

    def _note_change(self, summaries: list[str], version_id: str, op: dict | None) -> None:
        for summary in summaries:
            self.changes.append(summary)
            self.effects.append(summary)
        self.versions.append(version_id)
        self.effects.append(f"committed {version_id}")
        if op is not None:
            self.applied_ops.append(op)


def _delete_description(op: dict) -> str:
    if op.get("op") == "remove_edge":
        return f"remove edge {op.get('src')} → {op.get('dst')}"
    return f"remove node {op.get('id') or op.get('label')}"


def _is_secret(path: Path) -> bool:
    name = path.name.lower()
    if name in _SECRET_NAMES or name.startswith(".env"):
        return True
    return path.suffix.lower() in _SECRET_SUFFIXES


def _read_capped(path: Path, limit: int) -> tuple[bytes, bool]:
    with path.open("rb") as handle:
        blob = handle.read(limit + 1)
    truncated = len(blob) > limit
    return blob[:limit], truncated


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_agent(session: AgentSession, client) -> AgentReport:
    """Plan, act, check, until the checks pass or `session.max_steps` is hit.

    `client.complete(system=, messages=, tools=)` returns a `ModelTurn`.
    Tests pass a stub; the CLI passes `AnthropicToolClient`.
    """
    if session.max_steps < 1:
        raise ValueError("max_steps must be at least 1")

    messages: list[dict] = [{"role": "user", "content": session.opening_message()}]
    tools = tool_specs()
    stop_reason = "step_cap"
    steps_run = 0
    finish_nudged = False

    for step in range(1, session.max_steps + 1):
        steps_run = step
        print(dim(f"  · step {step}"))
        try:
            turn = client.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
        except (AssertionError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — a failed call ends the loop, not the process
            session.model_error = f"{type(exc).__name__}: {exc}"
            print(f"  ! model call failed: {session.model_error}")
            stop_reason = "model_error"
            break

        if turn.text:
            for line in turn.text.splitlines()[:12]:
                print(dim(f"  │ {line}"))

        if turn.tool_uses:
            pairs = _run_tools(session, turn.tool_uses)
            messages.append({"role": "assistant", "content": _assistant_blocks(turn)})
            messages.append(
                {
                    "role": "user",
                    "content": [_tool_result(tool, result) for tool, result in pairs],
                }
            )
            if session.finished:
                stop_reason = "finished"
                break
            continue

        gaps = audit_graph(session.store.current())
        if not gaps and (finish_nudged or step == session.max_steps):
            session.closing_text = turn.text
            stop_reason = "checks_passed"
            break
        if step == session.max_steps:
            break
        if not gaps:
            finish_nudged = True
            nudge = (
                "Checks pass. Call finish with a summary of what changed, "
                "any open uncertainties, and any questions for the user."
            )
            print(dim("  · checks clear — asking for the summary"))
        else:
            nudge = _gap_nudge(gaps)
            print(dim("  · checks still fail — continuing"))
        messages.append({"role": "assistant", "content": _assistant_blocks(turn)})
        messages.append({"role": "user", "content": nudge})

    report = _build_report(session, stop_reason, steps_run)
    print(render_report(report))
    report.artifacts = _persist(session, report)
    if report.artifacts:
        print(dim("  → " + ", ".join(report.artifacts)))
    else:
        print(dim("  → no artifacts written"))
    print()
    return report


def _run_tools(session: AgentSession, uses: list[ToolUse]) -> list[tuple[ToolUse, dict]]:
    # finish sees the graph after the other tools in this response.
    ordered = [tool for tool in uses if tool.name != "finish"] + [
        tool for tool in uses if tool.name == "finish"
    ]
    pairs = []
    for tool in ordered:
        arguments = tool.arguments if isinstance(tool.arguments, dict) else {}
        try:
            result = session.handle(tool.name, arguments)
        except (AssertionError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — a bad tool result is data, not a crash
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if tool.name in ACTION_TOOLS:
            result["checks"] = session.check_payload()
            _print_checks(result["checks"])
        elif tool.name == "finish" and not result.get("ok"):
            _print_checks(result.get("checks") or session.check_payload())
        pairs.append((tool, result))
    return pairs


def _assistant_blocks(turn: ModelTurn) -> list:
    if turn.assistant_content:
        return turn.assistant_content
    blocks: list[dict] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for tool in turn.tool_uses:
        blocks.append(
            {
                "type": "tool_use",
                "id": tool.id,
                "name": tool.name,
                "input": tool.arguments if isinstance(tool.arguments, dict) else {},
            }
        )
    return blocks or [{"type": "text", "text": "(empty)"}]


def _tool_result(tool: ToolUse, result: dict) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool.id,
        "content": json.dumps(result, ensure_ascii=False, default=str),
        "is_error": not result.get("ok", False),
    }


def _print_checks(payload: dict) -> None:
    gaps = payload.get("gaps") or []
    if not gaps:
        print(dim("  check: clear"))
        return
    for gap in gaps:
        print(f"  check: {gap['kind']} — {gap['detail']}")


def _gap_nudge(gaps: list[dict]) -> str:
    lines = ["Checks still fail. Keep acting until they pass or the step cap is reached.", ""]
    for gap in gaps:
        lines.append(f"- {gap['kind']}: {gap['detail']}")
    lines.append("")
    lines.append("Do not call finish while any gap remains.")
    return "\n".join(lines)


def _change_summary(changes: list[str]) -> str:
    if not changes:
        return "No graph changes were committed."
    return "Updated the graph: " + "; ".join(changes) + "."


def _build_report(session: AgentSession, stop_reason: str, steps: int) -> AgentReport:
    gaps = audit_graph(session.store.current())
    if session.model_error:
        summary = f"Stopped because the model call failed: {session.model_error}"
        uncertainties = [session.model_error, *[gap["detail"] for gap in gaps]]
        questions: list[str] = []
    elif session.finished:
        summary = session.summary
        uncertainties = list(session.uncertainties)
        questions = list(session.questions)
    elif stop_reason == "checks_passed":
        summary = (session.closing_text or "").strip() or _change_summary(session.changes)
        uncertainties = []
        questions = []
    elif not gaps:
        summary = (
            _change_summary(session.changes)
            + " The step cap was reached before finish was called; the graph checks are clear."
        )
        uncertainties = ["The model did not call finish before the step cap."]
        questions = []
    else:
        summary = (
            f"Stopped after {steps} step(s), the step cap, before the checks passed. "
            + _change_summary(session.changes)
        )
        uncertainties = [gap["detail"] for gap in gaps]
        questions = ["Which remaining gaps should be left unresolved?"]

    return AgentReport(
        goal=session.goal,
        plan=list(session.plan),
        summary=summary,
        uncertainties=uncertainties,
        questions=questions,
        changes=list(session.changes),
        gaps=gaps,
        checks_passed=not gaps,
        steps=steps,
        stop_reason=stop_reason,
        versions=list(session.versions),
    )


def render_report(report: AgentReport) -> str:
    """The closing recap: what changed, what is still open, what to ask."""
    lines = ["", "  ── summary", f"  {report.summary}", "", "  what changed"]
    if report.changes:
        lines.extend(f"    - {change}" for change in report.changes)
    else:
        lines.append("    - (nothing)")
    lines += ["", "  open uncertainties"]
    if report.uncertainties:
        lines.extend(f"    - {item}" for item in report.uncertainties)
    else:
        lines.append("    - (none)")
    lines += ["", "  questions"]
    if report.questions:
        lines.extend(f"    - {item}" for item in report.questions)
    else:
        lines.append("    - (none)")
    status = "passed" if report.checks_passed else "failed"
    lines += [
        "",
        f"  checks {status} after {report.steps} step(s) ({report.stop_reason})",
    ]
    return "\n".join(lines)


def exit_code(report: AgentReport) -> int:
    """0 when the graph checks passed. 1 when they did not, or the model failed."""
    if report.stop_reason == "model_error" or not report.checks_passed:
        return 1
    return 0


def _persist(session: AgentSession, report: AgentReport) -> list[str]:
    """Write the usual artifacts, append the trace, and save session state.

    `write_artifacts` lives in substrate.py, which imports this module. The
    import stays inside the function so the dependency points one way at load.
    """
    import substrate as app

    effects = list(session.effects)
    effects.append(f"stop: {report.stop_reason}")
    if report.uncertainties:
        effects.append("uncertainties: " + "; ".join(report.uncertainties))
    targets = ["json", "md", "uml", "python"]
    outputs = generators.generate(
        targets,
        session.store.current(),
        session.state,
        session.store.current_version(),
        session.store.current_branch(),
    )
    artifacts = app.write_artifacts(outputs, session.state, session.paths, effects)
    utterance = parser_mod.Utterance(
        raw=session.goal,
        intent_mode=session.state.intent_mode,
        content=session.goal,
        model_ops=session.applied_ops,
        suggested_targets=targets,
        notes=report.summary,
        source="agent",
    )
    trace.log_turn(
        session.paths["trace"],
        session.goal,
        utterance,
        session.store.current_version(),
        session.store.current_branch(),
        artifacts,
        session.state,
        effects,
    )
    session.state.save(session.paths["state"])
    return artifacts
