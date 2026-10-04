"""Free text -> a structured `Utterance`.

This is the only slow step in the app, and the only place the Anthropic API is
touched. Two things make it work:

  * The parser is given the *current state* every turn — mode, risk scope,
    focus, latest version, and the full lexicon — so context-dependent words
    ("this", "here", "current", "that") resolve to something real.
  * The lexicon is part of that context, which is what makes the language
    self-modifying: a term coined on turn 3 changes how turn 4 is understood.

`--dry-run` swaps the API parser for a rule-based one implementing the same
interface, so the rest of the app stays testable offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import config

# ---------------------------------------------------------------------------
# The Utterance
# ---------------------------------------------------------------------------

SCOPE_ACTIONS = ("none", "drill_in", "pop_out", "set_risk")
VERSION_ACTIONS = ("none", "lock_in", "walk_back", "branch", "describe")
SYSTEM_COMMANDS = (
    "none",
    "trace",
    "diff",
    "show_lexicon",
    "status",
    "merge",
    "switch",
    "help",
    "quit",
)
MODEL_OPS = (
    "add_node",
    "update_node",
    "remove_node",
    "add_edge",
    "remove_edge",
    "set_attr",
    "add_tag",
    "add_note",
    "set_meta",
)


@dataclass
class Utterance:
    """One turn, captured. `raw` is verbatim; everything else is inferred."""

    raw: str
    intent_mode: str
    scope_action: str = "none"
    scope_arg: str = ""
    version_action: str = "none"
    version_arg: str = ""
    system_command: str = "none"
    system_arg: str = ""
    content: str | None = None
    model_ops: list[dict] = field(default_factory=list)
    suggested_targets: list[str] = field(default_factory=list)
    coined_terms: list[dict] = field(default_factory=list)
    notes: str = ""
    source: str = "api"

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "intent_mode": self.intent_mode,
            "scope_action": self.scope_action,
            "scope_arg": self.scope_arg,
            "version_action": self.version_action,
            "version_arg": self.version_arg,
            "system_command": self.system_command,
            "system_arg": self.system_arg,
            "content": self.content,
            "model_ops": self.model_ops,
            "suggested_targets": self.suggested_targets,
            "coined_terms": self.coined_terms,
            "notes": self.notes,
            "source": self.source,
        }

    @property
    def is_pure_command(self) -> bool:
        return not self.content and not self.model_ops


@dataclass
class ParserContext:
    """The current state, handed to the parser so deixis resolves."""

    intent_mode: str
    risk_scope: str
    focus_key: str
    version_id: str
    branch: str
    lexicon_text: str
    model_summary: str


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

_OP_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": list(MODEL_OPS)},
        "id": {"type": "string"},
        "label": {"type": "string"},
        "kind": {"type": "string"},
        "description": {"type": "string"},
        "src": {"type": "string"},
        "dst": {"type": "string"},
        "key": {"type": "string"},
        "value": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": [
        "op", "id", "label", "kind", "description",
        "src", "dst", "key", "value", "text",
    ],
    "additionalProperties": False,
}

UTTERANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent_mode": {"type": "string", "enum": list(config.INTENT_MODES)},
        "scope_action": {"type": "string", "enum": list(SCOPE_ACTIONS)},
        "scope_arg": {"type": "string"},
        "version_action": {"type": "string", "enum": list(VERSION_ACTIONS)},
        "version_arg": {"type": "string"},
        "system_command": {"type": "string", "enum": list(SYSTEM_COMMANDS)},
        "system_arg": {"type": "string"},
        "content": {"type": "string"},
        "model_ops": {"type": "array", "items": _OP_SCHEMA},
        "suggested_targets": {
            "type": "array",
            "items": {"type": "string", "enum": list(config.TARGETS)},
        },
        "coined_terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "means": {"type": "string"},
                },
                "required": ["term", "means"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": [
        "intent_mode", "scope_action", "scope_arg",
        "version_action", "version_arg",
        "system_command", "system_arg",
        "content", "model_ops", "suggested_targets", "coined_terms", "notes",
    ],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """\
You are the parser for `substrate`, a hub where a person speaks plain-English
intent and it is captured as one clean structured representation. You do not
converse and you do not answer questions. You translate one utterance into one
structured record, and nothing else.

Two orthogonal dimensions are tracked at all times, and they are independent:

  * intent_mode — what the person is trying to do:
      ideation   exploring, proposing, thinking out loud
      evaluation weighing, comparing, checking, critiquing
      execution  committing to it, making it real, producing output
  * risk scope — how contained the consequences are (dev / staging /
    production). You never change intent_mode and risk scope from the same
    signal; they move independently.

If the utterance does not signal a mode, keep the current one. Only report a
different intent_mode when the utterance genuinely shifts what the person is
doing.

=== SYSTEM META-COMMANDS (a small, fixed set) ===

These are the only commands the system itself understands. The person will
phrase them naturally — map their phrasing onto the right one. Do not invent
new commands, and do not treat ordinary modelling talk as a command.

  version_action:
    lock_in    "lock that in", "seal it", "that's the one", "commit this"
               -> seals a milestone version
    walk_back  "walk it back to 0.0.5", "revert to v3", "go back to that
               earlier one"  -> version_arg = the version id, verbatim digits
    branch     "branch off that", "fork this", "try a variant called X"
               -> version_arg = a short branch name (slug), invent one from the
                  utterance if the person did not give a name
    describe   "what was 0.0.4", "describe that version"
               -> version_arg = the version id

  scope_action:
    drill_in   "drill into the engine", "let's work inside X", "focus on X"
               -> scope_arg = the thing being entered
    pop_out    "pop out", "back up a level", "come back out"
    set_risk   "we're in production now", "put me in staging", "back to dev"
               -> scope_arg = dev | staging | production

  system_command:
    trace        "show me the log", "how did we get here", "trace"
    diff         "what changed between 0.0.5 and 0.0.8" -> system_arg = "0.0.5 0.0.8"
    show_lexicon "show the lexicon", "what terms do I have"
    status       "where am I", "what's the current state"
    merge        "merge X back in" -> system_arg = branch name
    switch       "switch to branch X", "go back to main" -> system_arg = branch name
    help         "help", "what can I say"
    quit         "quit", "exit", "I'm done"

An utterance may carry a command AND modelling content at once ("add a cooling
stage and lock it in"). Fill in both. If it is a pure command, set content to
"".

=== OBJECT LANGUAGE (open-ended, grows through use) ===

Everything that is not a meta-command is modelling content: the person
describing their domain. Put the modelling statement in `content` verbatim
enough to be readable, and express its structure in `model_ops`.

The working model is a graph:
  nodes  — the things being modelled, each with an id, label, kind, description
  edges  — relations between nodes, each with src, dst, and a label
  notes  — statements that do not resolve into graph structure
  meta   — name and summary of the whole model

model_ops vocabulary:
  add_node     id, label, kind, description
  update_node  id (+ any of label/kind/description to change)
  remove_node  id
  add_edge     src, dst, label
  remove_edge  src, dst
  set_attr     id, key, value
  add_tag      id, value
  add_note     text
  set_meta     key ("name" or "summary"), value

Node ids are lowercase_snake_case, short and stable. Reuse the id of an
existing node when the person is clearly talking about it again — the current
model is listed for you below. Prefer update_node over add_node for something
that already exists.

EVERY op object must include EVERY field. Use an empty string "" for the fields
that op does not use.

=== COINING TERMS ===

When the utterance defines vocabulary — "when I say stage a unit, I mean create
a new System One node", "let's call that a spine", "by X I mean Y" — record it
in coined_terms. The lexicon below is injected into your context on every
subsequent turn, so once a term is coined you MUST honour it: a later utterance
using that term means what the lexicon says it means, and should produce the
model_ops the definition implies. This is the point of the system. Treat the
lexicon as binding.

A coinage may also carry out its own definition in the same utterance if the
person is using it, not just defining it.

=== SUGGESTED TARGETS ===

`suggested_targets` says which outputs make sense for THIS utterance. Do not
list all four by habit.
  json    the structured model — include whenever the model changed
  md      a readable narration — for summarising, reviewing, reporting
  uml     a Mermaid diagram — when structure or relations are the point
  python  a code scaffold — when the person wants the shape as code, or is in
          execution mode and building something
For a pure meta-command that changes no content, return an empty list.

=== NOTES ===

`notes` is one short sentence of your own rationale: what you took the
utterance to mean and why, especially any resolution of "this"/"here"/"that" or
any lexicon term you applied. This is read by a human later when they check
whether the capture drifted from what they meant. Be specific and brief.

Return only the structured record.
"""


def _build_user_block(raw: str, ctx: ParserContext) -> str:
    return f"""\
CURRENT STATE
  intent_mode:    {ctx.intent_mode}
  risk_scope:     {ctx.risk_scope}
  focus:          {ctx.focus_key}   (this is what "here" and "this level" mean)
  latest version: {ctx.version_id}
  branch:         {ctx.branch}

CURRENT LEXICON (coined by this person; binding on your reading)
{ctx.lexicon_text}

CURRENT MODEL
{ctx.model_summary}

UTTERANCE
{raw}"""


def model_summary(model: dict, limit: int = 40) -> str:
    """A compact view of the model for the parser's context."""
    nodes = model.get("nodes", {})
    edges = model.get("edges", [])
    if not nodes and not edges:
        return "(empty — nothing modelled yet)"
    lines = []
    for nid, node in sorted(nodes.items())[:limit]:
        kind = node.get("kind") or "unit"
        label = node.get("label") or nid
        lines.append(f"  node {nid}: {label} [{kind}] @{node.get('focus', 'root')}")
    if len(nodes) > limit:
        lines.append(f"  ... and {len(nodes) - limit} more nodes")
    for edge in edges[:limit]:
        label = f" ({edge['label']})" if edge.get("label") else ""
        lines.append(f"  edge {edge.get('src')} -> {edge.get('dst')}{label}")
    if len(edges) > limit:
        lines.append(f"  ... and {len(edges) - limit} more edges")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coercion — never trust the shape, always produce a valid Utterance
# ---------------------------------------------------------------------------


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def coerce(data: dict, raw: str, ctx: ParserContext, source: str) -> Utterance:
    data = data if isinstance(data, dict) else {}

    mode = _clean_str(data.get("intent_mode"))
    if mode not in config.INTENT_MODES:
        mode = ctx.intent_mode

    scope_action = _clean_str(data.get("scope_action")) or "none"
    if scope_action not in SCOPE_ACTIONS:
        scope_action = "none"

    version_action = _clean_str(data.get("version_action")) or "none"
    if version_action not in VERSION_ACTIONS:
        version_action = "none"

    system_command = _clean_str(data.get("system_command")) or "none"
    if system_command not in SYSTEM_COMMANDS:
        system_command = "none"

    ops = []
    for op in data.get("model_ops") or []:
        if isinstance(op, dict) and _clean_str(op.get("op")) in MODEL_OPS:
            ops.append({k: v for k, v in op.items() if isinstance(v, (str, int, float))})

    targets = [
        t for t in (data.get("suggested_targets") or []) if t in config.TARGETS
    ]
    # The structured model is the source of truth: if it changed, JSON follows.
    if ops and "json" not in targets:
        targets.insert(0, "json")

    coined = []
    for item in data.get("coined_terms") or []:
        if not isinstance(item, dict):
            continue
        term, means = _clean_str(item.get("term")), _clean_str(item.get("means"))
        if term and means:
            coined.append({"term": term, "means": means})

    content = _clean_str(data.get("content")) or None

    return Utterance(
        raw=raw,
        intent_mode=mode,
        scope_action=scope_action,
        scope_arg=_clean_str(data.get("scope_arg")),
        version_action=version_action,
        version_arg=_clean_str(data.get("version_arg")),
        system_command=system_command,
        system_arg=_clean_str(data.get("system_arg")),
        content=content,
        model_ops=ops,
        suggested_targets=targets,
        coined_terms=coined,
        notes=_clean_str(data.get("notes")),
        source=source,
    )


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a possibly-decorated response."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class NoCredentials(RuntimeError):
    """Raised at startup when nothing can authenticate the API call."""


def _credentials_available(client) -> bool:
    """Would a request actually authenticate? Checked before the first turn."""
    import os
    from pathlib import Path

    if getattr(client, "api_key", None) or getattr(client, "auth_token", None):
        return True
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK picks up on its own.
    return (Path.home() / ".config" / "anthropic").exists()


class Parser:
    name = "parser"

    def parse(self, raw: str, ctx: ParserContext) -> Utterance:  # pragma: no cover
        raise NotImplementedError


class ApiParser(Parser):
    """Language understanding via the Anthropic API."""

    name = "anthropic"

    def __init__(self, model: str = config.MODEL_ID):
        import anthropic  # imported lazily so --dry-run needs no SDK at all

        self._anthropic = anthropic
        self.model = model
        self.client = anthropic.Anthropic(
            timeout=config.REQUEST_TIMEOUT, max_retries=config.MAX_RETRIES
        )
        # The SDK resolves credentials lazily and would not complain until the
        # first parse. Check now so a missing key is a clear startup message
        # rather than a TypeError three turns in.
        if not _credentials_available(self.client):
            raise NoCredentials(
                "ANTHROPIC_API_KEY is not set (and no `ant auth login` profile was found)"
            )
        self._fallback = RuleParser()
        self._structured_ok = True

    def _request(self, raw: str, ctx: ParserContext):
        kwargs = {
            "model": self.model,
            "max_tokens": config.MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": _build_user_block(raw, ctx)}],
            "thinking": config.THINKING,
        }
        if self._structured_ok:
            kwargs["output_config"] = {
                "effort": config.EFFORT,
                "format": {"type": "json_schema", "schema": UTTERANCE_SCHEMA},
            }
        else:
            kwargs["messages"][0]["content"] += (
                "\n\nReturn ONLY a JSON object matching the record described above."
            )
        return self.client.messages.create(**kwargs)

    def parse(self, raw: str, ctx: ParserContext) -> Utterance:
        try:
            try:
                response = self._request(raw, ctx)
            except self._anthropic.BadRequestError:
                # Structured outputs unavailable for this model/account: drop
                # to plain JSON-in-text and stay in business.
                if not self._structured_ok:
                    raise
                self._structured_ok = False
                response = self._request(raw, ctx)

            text = next(
                (b.text for b in response.content if getattr(b, "type", "") == "text"),
                "",
            )
            data = _extract_json(text)
            if not data:
                raise ValueError("parser returned no usable JSON")
            return coerce(data, raw, ctx, source="anthropic")

        except Exception as exc:  # noqa: BLE001 — the REPL must survive anything
            utterance = self._fallback.parse(raw, ctx)
            utterance.source = "rule-fallback"
            reason = f"{type(exc).__name__}: {exc}"
            utterance.notes = (
                f"API parse failed ({reason}); fell back to rule-based parsing. "
                + utterance.notes
            )
            return utterance


class RuleParser(Parser):
    """Trivial rule-based parsing for --dry-run. No API, no network."""

    name = "rules"

    MODE_WORDS = {
        "ideation": ("ideation", "ideate", "brainstorm", "explore"),
        "evaluation": ("evaluation", "evaluate", "review", "assess", "critique"),
        "execution": ("execution", "execute", "build it", "make it real", "ship"),
    }

    def parse(self, raw: str, ctx: ParserContext) -> Utterance:
        text = raw.strip()
        low = text.lower()
        out: dict = {
            "intent_mode": ctx.intent_mode,
            "scope_action": "none",
            "scope_arg": "",
            "version_action": "none",
            "version_arg": "",
            "system_command": "none",
            "system_arg": "",
            "content": "",
            "model_ops": [],
            "suggested_targets": [],
            "coined_terms": [],
            "notes": "rule-based parse (no API)",
        }

        # --- system commands ---
        if re.fullmatch(r"(quit|exit|bye|q)\b.*", low):
            out["system_command"] = "quit"
            return coerce(out, raw, ctx, "rules")
        if re.fullmatch(r"help\b.*", low) or "what can i say" in low:
            out["system_command"] = "help"
            return coerce(out, raw, ctx, "rules")
        if "lexicon" in low or "what terms" in low:
            out["system_command"] = "show_lexicon"
            return coerce(out, raw, ctx, "rules")
        if low.startswith("trace") or "show me the log" in low or "how did we get here" in low:
            out["system_command"] = "trace"
            match = re.search(r"\b(\d+)\b", low)
            out["system_arg"] = match.group(1) if match else ""
            return coerce(out, raw, ctx, "rules")
        if low.startswith("status") or "where am i" in low:
            out["system_command"] = "status"
            return coerce(out, raw, ctx, "rules")
        diff_match = re.search(
            r"\b(?:diff|what changed between|compare)\b\D*(v?[\d.]+)\D+(v?[\d.]+)", low
        )
        if diff_match:
            out["system_command"] = "diff"
            out["system_arg"] = f"{diff_match.group(1)} {diff_match.group(2)}"
            return coerce(out, raw, ctx, "rules")
        merge_match = re.search(r"\bmerge\s+(?:branch\s+)?([\w-]+)", low)
        if merge_match:
            out["system_command"] = "merge"
            out["system_arg"] = merge_match.group(1)
            return coerce(out, raw, ctx, "rules")
        switch_match = re.search(r"\b(?:switch to|go back to|check out)\s+(?:branch\s+)?([\w-]+)$", low)
        if switch_match and "version" not in low:
            out["system_command"] = "switch"
            out["system_arg"] = switch_match.group(1)
            return coerce(out, raw, ctx, "rules")

        # --- risk scope ---
        risk_match = re.search(r"\b(dev|staging|production|prod)\b", low)
        if risk_match and re.search(
            r"(\b(risk|scope|switch|set|put me)\b|we'?re (in|on)|we are (in|on)|"
            r"mov(e|ing) (to|into)|\bback to\b|\bgo to\b|\bgoing to\b)",
            low,
        ):
            scope = risk_match.group(1)
            out["scope_action"] = "set_risk"
            out["scope_arg"] = "production" if scope == "prod" else scope
            return coerce(out, raw, ctx, "rules")

        # --- intent mode ---
        for mode, words in self.MODE_WORDS.items():
            if any(word in low for word in words) and re.search(
                r"\b(mode|switch|now|let's|lets|into|to)\b", low
            ):
                out["intent_mode"] = mode
                out["notes"] = f"rule-based parse: switched intent mode to {mode}"
                return coerce(out, raw, ctx, "rules")

        # --- focus ---
        drill = re.search(r"\b(?:drill in(?:to)?|focus on|go into|work inside)\s+(?:the\s+)?(.+)", low)
        if drill:
            out["scope_action"] = "drill_in"
            out["scope_arg"] = drill.group(1).strip(" .")
            return coerce(out, raw, ctx, "rules")
        if re.search(r"\b(pop out|back out|pop up|up one level|back up a level)\b", low):
            out["scope_action"] = "pop_out"
            return coerce(out, raw, ctx, "rules")

        # --- version actions ---
        back = re.search(r"\b(?:walk (?:it |that )?back|revert|roll back|go back)\b\D*([\d.]+)", low)
        if back:
            out["version_action"] = "walk_back"
            out["version_arg"] = back.group(1)
            return coerce(out, raw, ctx, "rules")
        describe = re.search(r"\b(?:describe|what was|what happened at)\b\D*(v?[\d.]+)", low)
        if describe:
            out["version_action"] = "describe"
            out["version_arg"] = describe.group(1)
            return coerce(out, raw, ctx, "rules")
        branch = re.search(r"\b(?:branch off|branch|fork)\b\s*(?:that|this|it)?\s*(?:as|called|named)?\s*([\w-]*)", low)
        if branch and re.search(r"\b(branch|fork)\b", low):
            out["version_action"] = "branch"
            out["version_arg"] = branch.group(1) or f"branch-{ctx.version_id.replace('.', '-')}"
            return coerce(out, raw, ctx, "rules")

        lock = bool(re.search(r"\b(lock (that |this |it )?in|seal it|commit this|that's the one)\b", low))
        if lock:
            out["version_action"] = "lock_in"
            # "lock that in" alone carries no content; strip the phrase if mixed.
            text = re.sub(
                r"\b(and )?(lock (that |this |it )?in|seal it|commit this)\b", "", text, flags=re.I
            ).strip(" .,and")
            low = text.lower()
            if not text:
                return coerce(out, raw, ctx, "rules")

        # --- coining a term ---
        coin = re.search(
            r"(?:when i say|by)\s+(?:a\s+)?[\"']?(.+?)[\"']?[,]?\s+i mean\s+(.+)", text, re.I
        ) or re.search(
            r"(?:let's call (?:that|this|it)|define)\s+[\"']?(.+?)[\"']?\s+(?:as|to mean)\s+(.+)", text, re.I
        ) or re.search(
            r"^[\"']?([\w \-]+?)[\"']?\s+means\s+(.+)", text, re.I
        )
        if coin:
            out["coined_terms"] = [
                {"term": coin.group(1).strip(" .\"'"), "means": coin.group(2).strip(" .")}
            ]
            out["notes"] = "rule-based parse: recorded a coinage"
            return coerce(out, raw, ctx, "rules")

        # --- modelling content ---
        out["content"] = text
        ops: list[dict] = []

        name_match = re.search(
            r"\b(?:call|name)\s+(?:the\s+)?(?:model|whole thing|system)\s+(.+)", text, re.I
        )
        if name_match:
            ops.append(_op("set_meta", key="name", value=name_match.group(1).strip(" .")))

        removal = re.search(
            r"^\s*(?:remove|delete|drop|get rid of)\s+(?:the\s+)?(?P<name>[\w \-]+?)"
            r"(?:\s+(?:node|component|unit|stage))?\s*$",
            text,
            re.I,
        )
        if removal:
            out["model_ops"] = [_op("remove_node", id=_snake(removal.group("name")))]
            out["suggested_targets"] = ["json", "md", "uml"]
            return coerce(out, raw, ctx, "rules")

        edge = re.search(
            r"^(.+?)\s+(feeds into|feeds|connects to|flows into|depends on|talks to|calls|sends to|links to)\s+(.+)$",
            text,
            re.I,
        )
        created = re.search(
            r"\b(?:create|add|make|stage|introduce|there's|there is)\b\s+(?:a|an|the)?\s*"
            r"(?P<kind>[\w-]+)?\s*(?:called|named)\s+(?P<name>[\w \-]+)",
            text,
            re.I,
        )
        if created:
            label = created.group("name").strip(" .")
            ops.append(
                _op(
                    "add_node",
                    id=_snake(label),
                    label=label,
                    kind=(created.group("kind") or "unit").lower(),
                    description=text,
                )
            )
        elif edge:
            src, label, dst = edge.group(1).strip(" ."), edge.group(2), edge.group(3).strip(" .")
            ops.append(_op("add_edge", src=_snake(src), dst=_snake(dst), label=label.lower()))
        elif re.match(r"^\s*(?:add|create|make|stage)\s+", text, re.I):
            label = re.sub(r"^\s*(?:add|create|make|stage)\s+(?:a|an|the)?\s*", "", text, flags=re.I)
            label = label.strip(" .")
            ops.append(
                _op("add_node", id=_snake(label), label=label, kind="unit", description=text)
            )
        else:
            ops.append(_op("add_note", text=text))

        out["model_ops"] = ops
        out["suggested_targets"] = ["json", "md"] + (
            ["uml"] if any(o["op"] in ("add_edge", "add_node") for o in ops) else []
        )
        if ctx.intent_mode == "execution":
            out["suggested_targets"].append("python")
        return coerce(out, raw, ctx, "rules")


def _op(op: str, **fields) -> dict:
    """Build a complete op object — every field present, blanks where unused."""
    base = {
        "op": op, "id": "", "label": "", "kind": "", "description": "",
        "src": "", "dst": "", "key": "", "value": "", "text": "",
    }
    base.update({k: str(v) for k, v in fields.items()})
    return base


def _snake(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return slug or "node"


# ---------------------------------------------------------------------------


def make_parser(dry_run: bool, model: str = config.MODEL_ID) -> tuple[Parser, str | None]:
    """Return (parser, warning). Falls back to rules if the API is unusable."""
    if dry_run:
        return RuleParser(), None
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return RuleParser(), (
            "the `anthropic` package is not installed — running with the "
            "rule-based parser. Install it, or pass --dry-run to silence this."
        )
    try:
        return ApiParser(model), None
    except NoCredentials as exc:
        return RuleParser(), (
            f"{exc}.\n"
            "    Falling back to the rule-based parser, so everything else still "
            "works — but phrasing must be literal.\n"
            "    Export ANTHROPIC_API_KEY for real language understanding, or pass "
            "--dry-run to make this explicit."
        )
    except Exception as exc:  # noqa: BLE001
        return RuleParser(), (
            f"could not start the Anthropic client ({type(exc).__name__}: {exc}). "
            "Running with the rule-based parser."
        )
