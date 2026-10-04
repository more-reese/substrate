"""Central configuration for substrate.

Everything tunable lives here: the model id, the sandbox path, the default
two-axis state, and the policy tables that make intent-mode and risk-scope
mean something concrete.
"""

from pathlib import Path

# --- Language understanding -------------------------------------------------

# The one place to change the model. Any current Claude model id works;
# `claude-opus-5` is the most capable if you want to trade latency for parse
# quality.
MODEL_ID = "claude-sonnet-5"

MAX_TOKENS = 4096

# Adaptive thinking lets Claude spend effort only on the genuinely ambiguous
# utterances; `effort: low` keeps the common case fast. The REPL should feel
# instant everywhere except this call.
THINKING = {"type": "adaptive"}
EFFORT = "low"

# Seconds. A parse that takes longer than this is a hung REPL, not a slow parse.
REQUEST_TIMEOUT = 45.0
MAX_RETRIES = 1

# --- Sandbox ----------------------------------------------------------------

# Every file substrate writes lives under here. Nothing is ever written
# outside this directory, regardless of what you drill into.
PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE = PROJECT_ROOT / "substrate_workspace"


def paths(workspace: Path) -> dict:
    """Resolve the workspace layout from a (possibly overridden) root."""
    return {
        "workspace": workspace,
        "artifacts": workspace / "artifacts",
        "versions": workspace / "versions",
        "lexicon": workspace / "lexicon.json",
        "trace": workspace / "trace.jsonl",
        "state": workspace / "state.json",
    }


# --- Version store ----------------------------------------------------------

# "automerge" is the intended default: a real CRDT with true branch/merge.
# "jsonlog" is the append-only-log fallback implementing the same interface.
VERSION_BACKEND = "automerge"

# --- The two orthogonal axes ------------------------------------------------

INTENT_MODES = ("ideation", "evaluation", "execution")
RISK_SCOPES = ("dev", "staging", "production")

DEFAULT_MODE = "ideation"
DEFAULT_RISK = "dev"

TARGETS = ("json", "md", "uml", "python")

# Intent mode decides how eagerly generated artifacts hit disk. JSON is the
# source of truth and is always written; the rest is graded by what you are
# actually trying to do.
WRITE_POLICY = {
    "ideation": {"json"},
    "evaluation": {"json", "md", "uml"},
    "execution": {"json", "md", "uml", "python"},
}

# Risk scope gates side effects. "write" = emitting an artifact file,
# "structural" = changing the shape of the model, "delete" = removing something.
CONFIRM_POLICY = {
    "dev": set(),
    "staging": {"write", "structural", "delete"},
    "production": {"write", "structural", "delete"},
}

# Scopes in which destructive model operations are refused outright.
NO_DELETE_SCOPES = {"production"}

# --- Presentation -----------------------------------------------------------

PREVIEW_LINES = 12
TRACE_DEFAULT_LIMIT = 15
