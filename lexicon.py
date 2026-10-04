"""The vocabulary that starts empty and grows.

This is the live self-modification mechanism. Terms you coin are written to
`lexicon.json` and injected into the parser's context on *every subsequent
turn*, so a definition made at turn 3 changes how turn 4 is understood. The
metalanguage edits the object language while the session is still running.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Lexicon:
    """A persistent, append-mostly map of coined term -> meaning."""

    def __init__(self, path: Path):
        self.path = path
        self.terms: dict[str, dict] = {}
        self.load()

    # --- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self.terms = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.terms = {}
            return
        raw_terms = data.get("terms", {})
        if isinstance(raw_terms, dict):
            self.terms = {
                str(k): v for k, v in raw_terms.items() if isinstance(v, dict)
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"terms": self.terms, "updated": _now()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # --- mutation ----------------------------------------------------------

    def add(self, term: str, means: str, version: str, raw: str) -> bool:
        """Record a coinage. Returns True if this changed the lexicon."""
        term = (term or "").strip()
        means = (means or "").strip()
        if not term or not means:
            return False
        existing = self.terms.get(term)
        if existing and existing.get("means") == means:
            return False
        self.terms[term] = {
            "means": means,
            "coined_at_version": version,
            "coined_at": _now(),
            "coined_by_utterance": raw,
            "redefines": existing.get("means") if existing else None,
        }
        self.save()
        return True

    # --- reading -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.terms)

    def render_for_prompt(self) -> str:
        """The block injected into the parser context every turn."""
        if not self.terms:
            return "(empty — no terms have been coined yet)"
        lines = []
        for term, entry in sorted(self.terms.items()):
            lines.append(f'- "{term}" means: {entry.get("means", "")}')
        return "\n".join(lines)

    def render_for_display(self) -> str:
        if not self.terms:
            return "  (empty — coin a term with: when I say X, I mean Y)"
        lines = []
        for term, entry in sorted(self.terms.items()):
            lines.append(f'  {term}')
            lines.append(f'      → {entry.get("means", "")}')
            coined = entry.get("coined_at_version")
            if coined:
                lines.append(f"      coined at {coined}")
            if entry.get("redefines"):
                lines.append(f'      redefined from: {entry["redefines"]}')
        return "\n".join(lines)
