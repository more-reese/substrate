#!/usr/bin/env python3
"""substrate — speak intent, capture structure, generate the rest.

A REPL. You type a plain-English sentence; it is captured as one clean
structured representation, tagged on two orthogonal axes (what you're trying to
do, and how contained the consequences are), versioned in a graph you can
operate on by voice, and rendered into whichever output formats that particular
utterance calls for.

The structured capture is the artifact. The outputs are disposable.

    python substrate.py             # real parsing via the Anthropic API
    python substrate.py --dry-run   # offline, rule-based parsing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config
import generators
import parser as parser_mod
import trace
import version_store as vs
from lexicon import Lexicon
from state import SessionState, artifact_dir

try:  # line editing and history, if the platform has it
    import readline  # noqa: F401
except ImportError:  # pragma: no cover
    pass


# Styling only when a terminal is actually watching, so a piped demo reads as
# a clean transcript.
TTY = sys.stdout.isatty()
DIM, BOLD, RESET = ("\033[2m", "\033[1m", "\033[0m") if TTY else ("", "", "")


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


HELP = """\
  substrate — everything below can be phrased naturally; these are just examples.

  SYSTEM META-COMMANDS (a small fixed set)
    lock it in                      seal a milestone version
    walk it back to 0.0.5           make an earlier version the base again
    branch off that as cooling      fork the version graph, switch to the fork
    switch to main                  move between branches
    merge cooling back in           merge a branch into this one (true CRDT merge)
    describe 0.0.4                  what utterance produced that version
    drill into the engine           namespace work + resolve "here" to it
    pop out                         back up one level
    switch to execution mode        set the intent axis (ideation/evaluation/execution)
    we're in staging now            set the risk axis (dev/staging/production)
    show me the log                 the full raw → parse → version → artifact trace
    diff 0.0.5 0.0.8                what changed between two versions
    show the lexicon                the vocabulary you have coined so far
    status                          where am I
    help / quit

  COINING VOCABULARY (this is the point)
    when I say stage a unit, I mean create a new System One node
    let's call that a spine
    by throughput I mean units cleared per hour
    Once coined, a term is injected into the parser's context every turn, so
    later utterances are read through it.

  EVERYTHING ELSE is object language: describe your domain and it becomes model
  structure. "there's a component called ignition", "ignition feeds cooling",
  "call the model Powertrain".
"""


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def write_artifacts(
    outputs: dict, state: SessionState, paths: dict, effects: list[str]
) -> list[str]:
    """Write the artifacts the current mode and risk scope allow.

    Intent mode decides how eagerly output hits disk; risk scope decides
    whether it needs your say-so first. Everything lands under the sandbox.
    """
    allowed = config.WRITE_POLICY.get(state.intent_mode, {"json"})
    to_write = {t: v for t, v in outputs.items() if t in allowed}
    previews = {t: v for t, v in outputs.items() if t not in allowed}

    written: list[str] = []
    if to_write:
        target_dir = artifact_dir(paths["artifacts"], state.focus)
        names = ", ".join(name for name, _ in to_write.values())
        if state.confirm(f"write {names} to {state.focus_label}", "write"):
            target_dir.mkdir(parents=True, exist_ok=True)
            for filename, text in to_write.values():
                path = target_dir / filename
                path.write_text(text, encoding="utf-8")
                written.append(str(path.relative_to(paths["workspace"])))
        else:
            effects.append(f"declined to write {names}")

    for target, (filename, text) in previews.items():
        lines = text.splitlines()
        head = "\n".join(f"    {line}" for line in lines[: config.PREVIEW_LINES])
        more = (
            f"\n    {dim(f'… {len(lines) - config.PREVIEW_LINES} more lines (not written in {state.intent_mode} mode)')}"
            if len(lines) > config.PREVIEW_LINES
            else ""
        )
        print(dim(f"  ┌ {filename} — preview only ({state.intent_mode} mode)"))
        print(head + more)
        print(dim("  └"))

    return written


# ---------------------------------------------------------------------------
# Meta-action handling
# ---------------------------------------------------------------------------


def apply_coinages(utterance, lex: Lexicon, store, effects: list[str]) -> None:
    for coinage in utterance.coined_terms:
        if lex.add(
            coinage["term"], coinage["means"], store.current_version(), utterance.raw
        ):
            effects.append(f'coined "{coinage["term"]}" → {coinage["means"]}')
            print(f'  ✎ lexicon: "{BOLD}{coinage["term"]}{RESET}" means {coinage["means"]}')
            print(dim(f"    ({len(lex)} term(s) now injected into every parse)"))


def apply_axes(utterance, state: SessionState, effects: list[str]) -> None:
    if state.set_mode(utterance.intent_mode):
        effects.append(f"intent_mode → {state.intent_mode}")
        print(f"  ◈ intent mode → {BOLD}{state.intent_mode}{RESET}")

    if utterance.scope_action == "set_risk":
        arg = utterance.scope_arg.strip().lower()
        arg = "production" if arg in ("prod", "production") else arg
        if state.set_risk(arg):
            effects.append(f"risk_scope → {state.risk_scope}")
            print(f"  ⚑ risk scope → {BOLD}{state.risk_scope}{RESET}")
            if state.deletes_forbidden():
                print(dim("    deletions are refused and everything needs confirmation here"))
        elif arg not in config.RISK_SCOPES:
            print(f"  ! not a risk scope: {utterance.scope_arg}")

    elif utterance.scope_action == "drill_in":
        if state.drill_in(utterance.scope_arg):
            effects.append(f"focus → {state.focus_key}")
            print(f"  ↳ focus → {BOLD}{state.focus_label}{RESET}")
            print(dim('    "here" and "this" now resolve to this level'))
        else:
            print("  ! nothing to drill into")

    elif utterance.scope_action == "pop_out":
        if state.pop_out():
            effects.append(f"focus → {state.focus_key}")
            print(f"  ↰ focus → {BOLD}{state.focus_label}{RESET}")
        else:
            print("  ! already at the root")


def apply_version_action(utterance, store, state: SessionState, effects: list[str]) -> None:
    action, arg = utterance.version_action, utterance.version_arg

    if action == "walk_back":
        if not state.confirm(f"walk back to {arg}", "structural"):
            effects.append("declined walk_back")
            return
        try:
            info = store.checkout(arg)
        except vs.VersionNotFound:
            print(f"  ! no such version: {arg}")
            return
        effects.append(f"walked back to {info.id}")
        print(f"  ⟲ walked back to {BOLD}{info.id}{RESET}")
        print(dim(f'    that version came from: "{info.raw}"'))
        print(dim("    what you say next builds from here"))

    elif action == "branch":
        name = arg or "variant"
        if not state.confirm(f"branch off {store.current_version()} as {name}", "structural"):
            effects.append("declined branch")
            return
        try:
            created = store.branch(name)
        except ValueError as exc:
            print(f"  ! {exc}")
            return
        effects.append(f"branched → {created}")
        print(f"  ⑂ branched off {store.current_version()} → {BOLD}{created}{RESET}")

    elif action == "describe":
        try:
            info = store.get_version(arg or store.current_version())
        except vs.VersionNotFound:
            print(f"  ! no such version: {arg}")
            return
        print(trace.render_describe(info, trace.read_turns(state._trace_path)))


def apply_system_command(utterance, store, state, lex, paths) -> bool:
    """Returns True if the REPL should exit."""
    command, arg = utterance.system_command, utterance.system_arg

    if command == "quit":
        return True

    if command == "help":
        print(HELP)

    elif command == "trace":
        limit = int(arg) if arg.isdigit() else config.TRACE_DEFAULT_LIMIT
        print(trace.render_trace(trace.read_turns(paths["trace"]), limit))

    elif command == "show_lexicon":
        print(f"  lexicon — {len(lex)} term(s), injected into every parse")
        print(lex.render_for_display())

    elif command == "status":
        print(f"  {state.summary()}")
        print(
            f"  branch={store.current_branch()}  version={store.current_version()}  "
            f"backend={store.backend_name}  branches={', '.join(store.branches())}"
        )
        print()
        print(trace.render_versions(store.list_versions(), store.current_version()))

    elif command == "diff":
        parts = arg.split()
        if len(parts) < 2:
            versions = store.list_versions()
            if len(versions) < 2:
                print("  ! need two versions to diff")
                return False
            parts = [versions[-2].id, versions[-1].id]
        try:
            print(trace.render_diff(store.diff(parts[0], parts[1])))
        except vs.VersionNotFound as exc:
            print(f"  ! no such version: {exc.args[0]}")

    elif command == "merge":
        if not arg:
            print("  ! which branch?")
        elif not state.confirm(f"merge {arg} into {store.current_branch()}", "structural"):
            print("  declined")
        else:
            try:
                changes = store.merge(arg)
            except ValueError as exc:
                print(f"  ! {exc}")
                return False
            print(f"  ⑃ merged {BOLD}{arg}{RESET} into {store.current_branch()}")
            print(trace.render_diff({"a": arg, "b": store.current_branch(), "changes": changes}))

    elif command == "switch":
        try:
            store.switch(arg)
        except ValueError as exc:
            print(f"  ! {exc}")
            return False
        print(f"  ⇄ on branch {BOLD}{store.current_branch()}{RESET} at {store.current_version()}")

    return False


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------


def run_turn(raw: str, ctx_parts) -> bool:
    """Process one utterance end to end. Returns True to exit the REPL."""
    store, state, lex, parser, paths = ctx_parts
    effects: list[str] = []

    context = parser_mod.ParserContext(
        intent_mode=state.intent_mode,
        risk_scope=state.risk_scope,
        focus_key=state.focus_key,
        version_id=store.current_version(),
        branch=store.current_branch(),
        lexicon_text=lex.render_for_prompt(),
        model_summary=parser_mod.model_summary(store.current()),
    )

    if TTY:
        print(dim("  · parsing…"), end="\r", flush=True)
    utterance = parser.parse(raw, context)
    if TTY:
        print(" " * 20, end="\r")

    if utterance.notes:
        print(dim(f"  ⟩ {utterance.notes}"))

    # 1. Vocabulary first — a coinage made now is binding on everything after.
    apply_coinages(utterance, lex, store, effects)

    # 2. The two axes.
    apply_axes(utterance, state, effects)

    # 3. Version-graph operations that change the base we build from.
    if utterance.version_action in ("walk_back", "branch", "describe"):
        apply_version_action(utterance, store, state, effects)

    # 4. System commands.
    if utterance.system_command != "none":
        if apply_system_command(utterance, store, state, lex, paths):
            return True

    # 5. Modelling content -> writes -> a version.
    version_id = store.current_version()
    artifacts: list[str] = []

    result = vs.ops_to_writes(
        store.current(),
        utterance.model_ops,
        focus_key=state.focus_key,
        allow_delete=not state.deletes_forbidden(),
    )
    for refusal in result.refusals:
        print(f"  ! {refusal}")
        effects.append(f"refused: {refusal}")

    lock_in = utterance.version_action == "lock_in"

    if result.writes:
        if state.confirm(f"apply {len(result.writes)} change(s) to the model", "structural"):
            store.update(result.writes)
            for summary in result.summaries:
                print(f"  + {summary}")
            info = store.commit(
                raw=raw,
                notes=utterance.notes,
                mode=state.intent_mode,
                scope=state.risk_scope,
                focus=state.focus_key,
                milestone=lock_in,
            )
            version_id = info.id
            effects.append(f"committed {info.id}")
            label = "  ★ locked in" if lock_in else "  ▸"
            print(f"{label} {BOLD}{info.id}{RESET}" + (" (milestone)" if lock_in else ""))

            targets = utterance.suggested_targets or ["json"]
            outputs = generators.generate(
                targets, store.current(), state, info.id, store.current_branch()
            )
            artifacts = write_artifacts(outputs, state, paths, effects)
            if artifacts:
                print(dim(f"  → {', '.join(artifacts)}"))
        else:
            effects.append("declined model change")
            print("  declined")

    elif lock_in:
        # "Lock that in" with nothing new to say: seal the current state.
        info = store.commit(
            raw=raw,
            notes=utterance.notes,
            mode=state.intent_mode,
            scope=state.risk_scope,
            focus=state.focus_key,
            milestone=True,
        )
        version_id = info.id
        effects.append(f"milestone {info.id}")
        print(f"  ★ locked in {BOLD}{info.id}{RESET} (milestone)")
        outputs = generators.generate(
            utterance.suggested_targets or ["json", "md"],
            store.current(),
            state,
            info.id,
            store.current_branch(),
        )
        artifacts = write_artifacts(outputs, state, paths, effects)
        if artifacts:
            print(dim(f"  → {', '.join(artifacts)}"))

    elif utterance.content and not utterance.model_ops:
        print(dim(f"  ⟩ captured, no structural change: {utterance.content}"))

    trace.log_turn(
        paths["trace"], raw, utterance, version_id, store.current_branch(), artifacts, state, effects
    )
    state.save(paths["state"])
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="substrate", description="Speak intent; capture structure; generate the rest."
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="skip the Anthropic API and use rule-based parsing (offline smoke test)",
    )
    ap.add_argument("--workspace", type=Path, default=config.WORKSPACE, help="sandbox directory")
    ap.add_argument(
        "--backend",
        choices=("automerge", "jsonlog"),
        default=config.VERSION_BACKEND,
        help="version store implementation (default: automerge)",
    )
    ap.add_argument("--model", default=config.MODEL_ID, help="Claude model id for parsing")
    ap.add_argument(
        "--yes",
        action="store_true",
        help="auto-confirm staging/production gates (for scripted demos)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = config.paths(args.workspace.resolve())
    paths["workspace"].mkdir(parents=True, exist_ok=True)

    store = vs.make_store(args.backend, paths["versions"])
    store.init()

    state = SessionState.load(paths["state"], assume_yes=args.yes)
    state._trace_path = paths["trace"]  # for `describe`
    lex = Lexicon(paths["lexicon"])

    parser, warning = parser_mod.make_parser(args.dry_run, args.model)

    print()
    print(f"{BOLD}substrate{RESET} — the capture is the artifact; the outputs are disposable.")
    print(
        dim(
            f"  parser={parser.name}"
            + (f" ({args.model})" if parser.name == "anthropic" else "")
            + f"  versions={store.backend_name}  workspace={paths['workspace']}"
        )
    )
    print(dim(f"  lexicon: {len(lex)} term(s)   ·   type 'help' for what you can say"))
    if warning:
        print(f"  ! {warning}")
    if args.dry_run:
        print(dim("  --dry-run: rule-based parsing only. Phrasing must be fairly literal."))
    print()

    interactive = sys.stdin.isatty()
    while True:
        prompt = state.prompt(store.current_version(), store.current_branch())
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not interactive:
            print(line)  # echo so a piped demo reads as a transcript
        if not line.strip():
            continue
        try:
            if run_turn(line.strip(), (store, state, lex, parser, paths)):
                break
        except Exception as exc:  # noqa: BLE001 — a bad turn must not kill the session
            print(f"  ! turn failed: {type(exc).__name__}: {exc}")
        print()

    state.save(paths["state"])
    print(dim(f"  state saved · {store.current_version()} on {store.current_branch()}"))
    print("  bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
