"""The two orthogonal axes, plus the focus pointer.

Intent mode (what I'm trying to do) and risk scope (how contained the
consequences are) run at all times and are independent of each other. Focus is
a third, smaller thing: a pointer that namespaces generated artifacts and gives
the parser a referent for "here" and "this".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import config

_SEGMENT_RE = re.compile(r"[^a-z0-9_-]+")


def slugify(text: str) -> str:
    """Collapse arbitrary prose into one safe path segment."""
    slug = _SEGMENT_RE.sub("-", (text or "").strip().lower()).strip("-")
    return slug or "unnamed"


def slug_path(text: str) -> list[str]:
    """Split "engine/ignition" or "the engine room" into safe segments."""
    parts = [p for p in re.split(r"[/\\.>]+", text or "") if p.strip()]
    return [slugify(p) for p in parts] or [slugify(text)]


@dataclass
class SessionState:
    """Mode, scope, and focus — the state carried between turns."""

    intent_mode: str = config.DEFAULT_MODE
    risk_scope: str = config.DEFAULT_RISK
    focus: list[str] = field(default_factory=list)
    assume_yes: bool = False

    # --- rendering ---------------------------------------------------------

    @property
    def focus_label(self) -> str:
        return "@" + "/".join(["root", *self.focus])

    @property
    def focus_key(self) -> str:
        """The focus as the parser and the model see it: "root" or "root/x"."""
        return "/".join(["root", *self.focus])

    def prompt(self, version_id: str, branch: str = "main") -> str:
        marker = version_id if branch == "main" else f"{branch}:{version_id}"
        return f"[{self.intent_mode} · {self.risk_scope} · {self.focus_label} · {marker}] › "

    def summary(self) -> str:
        return (
            f"intent_mode={self.intent_mode}  risk_scope={self.risk_scope}  "
            f"focus={self.focus_label}"
        )

    # --- axis transitions --------------------------------------------------

    def set_mode(self, mode: str) -> bool:
        if mode in config.INTENT_MODES and mode != self.intent_mode:
            self.intent_mode = mode
            return True
        return False

    def set_risk(self, scope: str) -> bool:
        if scope in config.RISK_SCOPES and scope != self.risk_scope:
            self.risk_scope = scope
            return True
        return False

    def drill_in(self, target: str) -> bool:
        segments = slug_path(target)
        if not segments:
            return False
        self.focus.extend(segments)
        return True

    def pop_out(self) -> bool:
        if not self.focus:
            return False
        self.focus.pop()
        return True

    # --- risk gating -------------------------------------------------------

    def needs_confirmation(self, kind: str) -> bool:
        return kind in config.CONFIRM_POLICY.get(self.risk_scope, set())

    def deletes_forbidden(self) -> bool:
        return self.risk_scope in config.NO_DELETE_SCOPES

    def confirm(self, description: str, kind: str) -> bool:
        """Ask before a gated side effect. dev never asks; production always does."""
        if not self.needs_confirmation(kind):
            return True
        if self.assume_yes:
            print(f"  ⟡ [{self.risk_scope}] auto-confirmed: {description}")
            return True
        try:
            answer = input(f"  ⟡ [{self.risk_scope}] {description} — proceed? [y/N] ")
        except EOFError:
            print(f"  ⟡ declined (no input available): {description}")
            return False
        return answer.strip().lower() in ("y", "yes")

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "intent_mode": self.intent_mode,
            "risk_scope": self.risk_scope,
            "focus": list(self.focus),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, assume_yes: bool = False) -> "SessionState":
        state = cls(assume_yes=assume_yes)
        if not path.exists():
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return state
        if data.get("intent_mode") in config.INTENT_MODES:
            state.intent_mode = data["intent_mode"]
        if data.get("risk_scope") in config.RISK_SCOPES:
            state.risk_scope = data["risk_scope"]
        focus = data.get("focus")
        if isinstance(focus, list):
            state.focus = [slugify(str(f)) for f in focus if str(f).strip()]
        return state


def artifact_dir(artifacts_root: Path, focus: list[str]) -> Path:
    """Where artifacts for the current focus go — always inside the sandbox.

    The focus pointer namespaces output, but it can never escape: the resolved
    path is checked against the artifacts root before it is handed back.
    """
    root = artifacts_root.resolve()
    candidate = root.joinpath(*[slugify(seg) for seg in focus]).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"refusing to write outside the sandbox: {candidate}")
    return candidate
