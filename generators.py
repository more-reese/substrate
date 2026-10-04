"""Emitters: one clean representation in, several disposable outputs out.

Nothing here is precious. The JSON model in the version store is the artifact;
everything these functions produce can be thrown away and regenerated. They are
pure string transforms, so producing all four together costs nothing
measurable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

FILENAMES = {
    "json": "model.json",
    "md": "model.md",
    "uml": "model.mmd",
    "python": "model_stub.py",
}


def _identifier(text: str, fallback: str = "Node") -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", text or "")
    name = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not name or not name[0].isalpha():
        name = fallback + name
    return name


def _snake(text: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"{fallback}_{slug}" if slug else fallback
    return slug


def _sorted_nodes(model: dict) -> list[tuple[str, dict]]:
    return sorted(model.get("nodes", {}).items())


# ---------------------------------------------------------------------------


def gen_json(model: dict, **_) -> str:
    """The structured representation itself — the source of truth."""
    return json.dumps(model, indent=2, sort_keys=True) + "\n"


def gen_markdown(model: dict, state=None, version_id: str = "", branch: str = "main", **_) -> str:
    """A readable narration of the current model and state."""
    meta = model.get("meta", {})
    nodes = _sorted_nodes(model)
    edges = model.get("edges", [])
    notes = model.get("notes", [])

    lines = [f"# {meta.get('name') or 'untitled'}", ""]
    if meta.get("summary"):
        lines += [meta["summary"], ""]

    lines += ["| | |", "|---|---|"]
    lines.append(f"| version | `{version_id}` |")
    lines.append(f"| branch | `{branch}` |")
    if state is not None:
        lines.append(f"| intent mode | {state.intent_mode} |")
        lines.append(f"| risk scope | {state.risk_scope} |")
        lines.append(f"| focus | `{state.focus_label}` |")
    lines.append(
        f"| generated | {datetime.now(timezone.utc).isoformat(timespec='seconds')} |"
    )
    lines.append("")

    for key, value in sorted(meta.items()):
        if key in ("name", "summary", "created"):
            continue
        lines.append(f"- **{key}**: {value}")
    if len(lines) and lines[-1].startswith("- **"):
        lines.append("")

    lines += [f"## Units ({len(nodes)})", ""]
    if not nodes:
        lines.append("_Nothing modelled yet._")
    for nid, node in nodes:
        kind = node.get("kind") or "unit"
        lines.append(f"### {node.get('label') or nid}")
        lines.append(f"`{nid}` · kind: **{kind}** · focus: `{node.get('focus', 'root')}`")
        if node.get("description"):
            lines.append("")
            lines.append(node["description"])
        attrs = node.get("attrs") or {}
        if attrs:
            lines.append("")
            for key, value in sorted(attrs.items()):
                lines.append(f"- {key}: {value}")
        if node.get("tags"):
            lines.append("")
            lines.append("tags: " + ", ".join(f"`{t}`" for t in node["tags"]))
        lines.append("")

    lines += [f"## Relations ({len(edges)})", ""]
    if not edges:
        lines.append("_No relations yet._")
    for edge in edges:
        label = f" — {edge['label']}" if edge.get("label") else ""
        lines.append(f"- `{edge.get('src')}` → `{edge.get('dst')}`{label}")
    lines.append("")

    if notes:
        lines += [f"## Notes ({len(notes)})", ""]
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)


def gen_mermaid(model: dict, **_) -> str:
    """Mermaid graph syntax — text-based, renders anywhere."""
    nodes = _sorted_nodes(model)
    edges = model.get("edges", [])

    lines = ["graph TD"]
    if not nodes:
        lines.append('    empty["(nothing modelled yet)"]')
        return "\n".join(lines) + "\n"

    # Group by focus so drilling in shows up in the diagram.
    by_focus: dict[str, list] = {}
    for nid, node in nodes:
        by_focus.setdefault(node.get("focus") or "root", []).append((nid, node))

    def emit_node(nid: str, node: dict, indent: str) -> str:
        label = (node.get("label") or nid).replace('"', "'")
        kind = (node.get("kind") or "").replace('"', "'")
        text = f"{label}<br/><i>{kind}</i>" if kind else label
        return f'{indent}{nid}["{text}"]'

    for focus, group in sorted(by_focus.items()):
        if focus in ("root", ""):
            for nid, node in group:
                lines.append(emit_node(nid, node, "    "))
        else:
            sub = _snake(focus, "focus")
            lines.append(f'    subgraph {sub}["{focus}"]')
            for nid, node in group:
                lines.append(emit_node(nid, node, "        "))
            lines.append("    end")

    for edge in edges:
        src, dst = edge.get("src"), edge.get("dst")
        if not src or not dst:
            continue
        label = (edge.get("label") or "").replace('"', "'")
        if label:
            lines.append(f"    {src} -->|{label}| {dst}")
        else:
            lines.append(f"    {src} --> {dst}")

    return "\n".join(lines) + "\n"


def gen_python(model: dict, **_) -> str:
    """A scaffold reflecting the model's structure — a stub, not a program."""
    meta = model.get("meta", {})
    nodes = _sorted_nodes(model)
    edges = model.get("edges", [])

    lines = [
        '"""Generated by substrate — do not edit.',
        "",
        f"Model: {meta.get('name') or 'untitled'}",
        "This file is disposable; regenerate it from the structured model.",
        '"""',
        "",
        "from dataclasses import dataclass, field",
        "",
    ]

    if not nodes:
        lines += ["", "# Nothing modelled yet.", ""]
        return "\n".join(lines)

    class_names: dict[str, str] = {}
    for nid, node in nodes:
        cls = _identifier(node.get("label") or nid)
        # Keep class names unique even if two labels collapse to the same name.
        if cls in class_names.values():
            cls = f"{cls}{_identifier(nid)}"
        class_names[nid] = cls

    for nid, node in nodes:
        cls = class_names[nid]
        lines.append("")
        lines.append("@dataclass")
        lines.append(f"class {cls}:")
        doc = node.get("description") or node.get("label") or nid
        lines.append(f'    """{doc}"""')
        lines.append("")
        lines.append(f'    kind: str = "{node.get("kind") or "unit"}"')
        lines.append(f'    substrate_id: str = "{nid}"')
        for key, value in sorted((node.get("attrs") or {}).items()):
            lines.append(f'    {_snake(key, "attr")}: str = "{str(value)}"')
        if node.get("tags"):
            tags = ", ".join(f'"{t}"' for t in node["tags"])
            lines.append(
                f"    tags: list[str] = field(default_factory=lambda: [{tags}])"
            )
        lines.append("")

    lines.append("")
    lines.append("def wire() -> list[tuple[str, str, str]]:")
    lines.append('    """The relations declared in the model."""')
    if edges:
        lines.append("    return [")
        for edge in edges:
            lines.append(
                f'        ("{edge.get("src")}", "{edge.get("dst")}", "{edge.get("label") or ""}"),'
            )
        lines.append("    ]")
    else:
        lines.append("    return []")
    lines.append("")

    return "\n".join(lines)


GENERATORS = {
    "json": gen_json,
    "md": gen_markdown,
    "uml": gen_mermaid,
    "python": gen_python,
}


def generate(targets, model: dict, state=None, version_id: str = "", branch: str = "main") -> dict:
    """Produce every requested target together.

    Returns {target: (filename, text)}. Unknown targets are ignored rather than
    raising — the parser suggests, it does not command.
    """
    wanted = [t for t in targets if t in GENERATORS]
    return {
        target: (
            FILENAMES[target],
            GENERATORS[target](
                model, state=state, version_id=version_id, branch=branch
            ),
        )
        for target in wanted
    }
