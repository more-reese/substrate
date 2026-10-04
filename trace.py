"""Inspectable execution.

Every turn is logged as `raw utterance -> parsed Utterance -> resulting version
-> artifacts written`. That lineage is the point: you can always look back at
how a sentence became structure, see where the capture drifted from what you
meant, and correct it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_turn(
    path: Path,
    raw: str,
    utterance,
    version_id: str,
    branch: str,
    artifacts: list[str],
    state,
    effects: list[str],
) -> None:
    """Append one turn to the trace log."""
    record = {
        "ts": _now(),
        "raw": raw,
        "parsed": utterance.to_dict(),
        "version": version_id,
        "branch": branch,
        "artifacts": artifacts,
        "effects": effects,
        "state_after": state.to_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def read_turns(path: Path) -> list[dict]:
    if not path.exists():
        return []
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return turns


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def render_trace(turns: list[dict], limit: int = config.TRACE_DEFAULT_LIMIT) -> str:
    if not turns:
        return "  (nothing traced yet)"
    shown = turns[-limit:]
    lines = [
        f"  showing {len(shown)} of {len(turns)} turn(s) — "
        "raw → parse → version → artifacts",
        "",
    ]
    for index, turn in enumerate(turns[len(turns) - len(shown) :], start=len(turns) - len(shown) + 1):
        parsed = turn.get("parsed", {})
        lines.append(f"  ── turn {index} · {turn.get('ts', '')}")
        lines.append(f'     raw       "{turn.get("raw", "")}"')
        lines.append(
            f"     axes      {parsed.get('intent_mode', '?')} · "
            f"{turn.get('state_after', {}).get('risk_scope', '?')} · "
            f"@{'/'.join(['root', *turn.get('state_after', {}).get('focus', [])])}"
        )

        actions = []
        for key in ("scope_action", "version_action", "system_command"):
            value = parsed.get(key, "none")
            if value and value != "none":
                arg = parsed.get(key.replace("action", "arg").replace("command", "arg"), "")
                actions.append(f"{value}({arg})" if arg else value)
        if actions:
            lines.append(f"     actions   {', '.join(actions)}")

        ops = parsed.get("model_ops") or []
        if ops:
            summary = ", ".join(
                f"{o.get('op')}:{o.get('id') or o.get('src') or o.get('key') or ''}".rstrip(":")
                for o in ops[:6]
            )
            extra = f" (+{len(ops) - 6} more)" if len(ops) > 6 else ""
            lines.append(f"     ops       {summary}{extra}")

        coined = parsed.get("coined_terms") or []
        if coined:
            lines.append(
                "     coined    "
                + ", ".join(f'"{c.get("term")}" = {c.get("means")}' for c in coined)
            )

        if parsed.get("notes"):
            lines.append(f"     why       {parsed['notes']}")
        lines.append(
            f"     version   {turn.get('version', '?')}"
            + (f"  on {turn['branch']}" if turn.get("branch") and turn["branch"] != "main" else "")
            + f"  [parsed by {parsed.get('source', '?')}]"
        )
        artifacts = turn.get("artifacts") or []
        lines.append(
            f"     artifacts {', '.join(artifacts) if artifacts else '(none written)'}"
        )
        for effect in turn.get("effects") or []:
            lines.append(f"     ·         {effect}")
        lines.append("")
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "∅"
    text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 70 else text[:67] + "..."


def render_diff(result: dict) -> str:
    changes = result.get("changes") or []
    lines = [f"  {result.get('a')} → {result.get('b')}"]
    if result.get("detail"):
        lines.append(f"  ({result['detail']})")
    lines.append("")
    if not changes:
        lines.append("  (identical)")
        return "\n".join(lines)
    marks = {"added": "+", "removed": "-", "changed": "~"}
    for change in changes:
        mark = marks.get(change["op"], "?")
        if change["op"] == "changed":
            lines.append(
                f"  {mark} {change['path']}: {_fmt(change['before'])} → {_fmt(change['after'])}"
            )
        elif change["op"] == "added":
            lines.append(f"  {mark} {change['path']}: {_fmt(change['after'])}")
        else:
            lines.append(f"  {mark} {change['path']}: {_fmt(change['before'])}")
    lines.append("")
    lines.append(f"  {len(changes)} change(s)")
    return "\n".join(lines)


def render_describe(info, turns: list[dict]) -> str:
    """v1: the stored raw utterance plus the parser's notes. Provenance, not prose."""
    lines = [
        f"  {info.id}" + ("  ★ milestone" if info.milestone else ""),
        f"  branch    {info.branch}   parent {info.parent or '—'}",
        f"  captured  {info.ts}",
        f"  axes      {info.mode or '—'} · {info.scope or '—'} · @{info.focus}",
        "",
        f'  raw       "{info.raw}"',
    ]
    if info.notes:
        lines.append(f"  why       {info.notes}")
    if info.heads:
        lines.append(f"  heads     {', '.join(h[:12] for h in info.heads)}")

    matching = [t for t in turns if t.get("version") == info.id]
    if matching:
        parsed = matching[-1].get("parsed", {})
        ops = parsed.get("model_ops") or []
        if ops:
            lines.append("")
            lines.append("  ops applied at this version:")
            for op in ops:
                detail = op.get("id") or f"{op.get('src')}→{op.get('dst')}" or op.get("key")
                lines.append(f"    - {op.get('op')} {detail or ''}".rstrip())
    return "\n".join(lines)


def render_versions(versions: list, current: str) -> str:
    if not versions:
        return "  (no versions yet)"
    lines = []
    for info in versions:
        marker = "→" if info.id == current else " "
        star = " ★" if info.milestone else ""
        branch = "" if info.branch == "main" else f" [{info.branch}]"
        raw = info.raw if len(info.raw) <= 52 else info.raw[:49] + "..."
        lines.append(f"  {marker} {info.id:<10}{star:<2}{branch:<12} {raw}")
    return "\n".join(lines)
