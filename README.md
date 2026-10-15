# substrate

A command-line REPL that turns plain-English sentences into one structured, versioned graph model, and generates JSON, Markdown, Mermaid and Python stubs from it. Agent mode takes a goal instead of a sentence and builds that graph in a plan, act, and check loop.

<!-- ![Terminal session: a component added, an edge implying a second node, "lock it in" sealing v0.1.0](docs/screenshots/repl-session.png) -->
<!-- ![The trace view after the demo script: raw utterance, parsed actions, version and artifacts per turn](docs/screenshots/trace.png) -->

```
[ideation · dev · @root · v0.0.1] › ignition feeds into cooling
  + implied node cooling from an edge
  + linked ignition → cooling (feeds into)
  ▸ v0.0.2
  → artifacts/model.json
```

## Why I built this

I think out loud, often away from a keyboard. I wanted to describe a system in plain English and have it captured as a structured model I can version, inspect and generate from, instead of a transcript I'd have to re-read. The structured capture is the artifact; the outputs are disposable and can be regenerated at any time. Because it's an agent that writes files, I also wanted it to know the difference between me thinking aloud and me committing to something, and between a sandbox and something that matters.

## What it does

- Turns each sentence into a parsed record: intent mode, scope and version actions, a system command, model operations (`add_node`, `add_edge`, `set_attr`, `add_note`, and so on), suggested outputs, and any newly coined terms.
- `substrate agent "goal"` runs a separate plan → act → check loop. Claude calls the same graph and version operations as tools, plus read-only tools (`inspect_graph`, `find_orphans`, `diff_versions`, `read_file`). After every change, code checks the graph for orphan nodes, missing edges, contradictions, and nodes an edge only implied. It stops when the checks pass or after 20 steps, then prints what changed, what is still uncertain, and what only you can answer, and writes the usual outputs.
- Applies those operations to a single graph model (nodes, edges, notes, metadata) and commits a version that carries the sentence that produced it.
- Generates `model.json`, `model.md`, `model.mmd` (Mermaid) and `model_stub.py` (dataclass scaffolding) from the model. How many of them reach disk depends on the intent mode.
- Tracks two independent settings on every turn, intent mode and risk scope, which control what gets written and what needs your confirmation.
- Maps plain-language version operations ("lock it in", "walk it back to 0.0.2", "branch off that as coldstart", "merge coldstart back in") onto an Automerge CRDT, with a JSON-log fallback.
- Grows a domain vocabulary from use. "When I say X, I mean Y" is stored and injected into the parser's context on every later turn.
- Logs every turn as raw sentence → parsed record → version → files written, viewable with `show me the log`, `describe 0.0.2` and `diff 0.0.1 0.1.1`.
- Works offline with a rule-based parser and no API key.

## Use cases

- Sketching a system or business process by talking it through, one sentence at a time, and ending up with a graph instead of notes.
- Turning a rambling idea into a structured spec: components, relations and open notes, with the original phrasing kept on every version.
- Generating a first-pass Mermaid diagram and a Python data model from a description.
- Keeping a versioned record of how a model evolved, including the branches you tried and abandoned.

## Quick start

### Requirements

- Python 3. I verified it on Python 3.11.7 on macOS 15.8 (Apple Silicon).
- `automerge==1.0.0rc1`, which ships prebuilt wheels for macOS (arm64 and x86_64), Linux and Windows. If it won't install on your platform, run with `--backend jsonlog`.
- Optional: an Anthropic API key for real language understanding.

### Install and run

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python substrate.py --dry-run
```

`--dry-run` uses the offline rule-based parser. To use Claude:

```bash
export ANTHROPIC_API_KEY=...
```

```bash
.venv/bin/python substrate.py
```

Agent mode is a different command. It needs the API key; `--dry-run` does not apply to it.

```bash
.venv/bin/python substrate.py agent "map a cooling loop: ignition feeds cooling"
```

Everything is written under `substrate_workspace/` in the repo folder, unless you point `--workspace` somewhere else.

| Flag | Does |
| --- | --- |
| `--dry-run` | Skip the API and use rule-based parsing |
| `--model ID` | Override the Claude model for this run (default in `config.py`) |
| `--backend automerge\|jsonlog` | Pick the version store |
| `--workspace PATH` | Use a different sandbox directory |
| `--yes` | Auto-confirm staging and production prompts (for scripted demos) |
| `--max-steps N` | Agent mode only. Stop after N model calls (default 20) |
| `--docs DIR` | Agent mode only. Extra directory `read_file` may read (repeatable) |

### A scripted demo

This runs offline and shows most of the features. Paste the lines one at a time into a `--dry-run` session, or pipe them in:

```bash
printf '%s\n' "add a component called ignition" "ignition feeds into cooling" "lock it in" "when I say a spine, I mean the load-bearing path through the system" "drill into the engine bay" "switch to execution mode" "add a component called exhaust" "walk it back to 0.0.2" "diff 0.0.1 0.1.1" "branch off that as coldstart" "show me the log" | .venv/bin/python substrate.py --dry-run --yes
```

| Line | Shows |
| --- | --- |
| 1–2 | Sentences becoming graph structure; `cooling` is implied by an edge before it is described |
| 3 | Locking a milestone: the minor version bumps to `v0.1.0` |
| 4 | Coining a term, written to the lexicon |
| 5 | Scoping: focus becomes `@root/engine-bay`, and artifacts are written under `artifacts/engine-bay/` |
| 6 | Intent mode change: in execution mode all four formats are written |
| 7 | Multi-format generation: JSON, Markdown, Mermaid and Python together |
| 8 | Walking back: `v0.0.2` becomes the base, and the sentence that produced it is shown |
| 9 | A path-level diff plus Automerge's own patch count |
| 10 | Branching: a real Automerge fork; the prompt becomes `coldstart:v0.0.2` |
| 11 | The full trace |

### Configuration

| Variable | Required | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Agent mode | Enables Claude parsing and agent mode. Without it, the REPL warns and falls back to the rule-based parser. `substrate agent` exits. |
| `ANTHROPIC_AUTH_TOKEN` | No | Alternative credential the Anthropic SDK accepts. |

substrate doesn't read a `.env` file; export the variables in your shell. See [`.env.example`](.env.example). The model ID, sandbox path, and the write and confirmation policies are constants in [`config.py`](config.py).

### Running without an API key

With `--dry-run`, or with no key set, the parser is a set of regular expressions. It handles the meta-commands, the coining patterns ("when I say X, I mean Y", "let's call that X", "X means Y"), and a few modeling shapes: "add/create a component called X", "X feeds into / connects to / depends on Y", "call the model X", and "remove X". Anything else is stored as a note. Phrasing has to be fairly literal. Versioning, branching, merging, generation, the trace and the risk gates all work exactly as they do with the API.

Agent mode does not have an offline fallback. `substrate agent` with `--dry-run`, or with no API key, exits with a message and leaves the REPL path alone.

### Tests

```bash
python3 -m unittest discover -s tests
```

The agent tests stub the model: they never call the API. A second file checks that the rule-based parser and `run_turn` still commit a node, and that `substrate.py --dry-run` still runs.

## How it works

```mermaid
flowchart TD
    U["sentence"] --> P["parser<br/>(Claude or rules)"]
    C["current state:<br/>mode, risk scope, focus,<br/>version, lexicon, model summary"] --> P
    P --> R["parsed record"]
    R --> X["lexicon<br/>(coined terms)"]
    R --> A["mode / risk / focus"]
    R --> V["version ops:<br/>lock · walk back · branch · merge"]
    R --> O["model ops → path-level writes"]
    O --> G{"risk gate:<br/>confirm? deletes allowed?"}
    G --> S[("VersionStore<br/>Automerge or JSON log")]
    S --> W["generators:<br/>json · md · mermaid · python"]
    W --> M{"write policy<br/>by intent mode"}
    M --> F[("substrate_workspace/artifacts/")]
    R --> T[("trace.jsonl")]
```

Start with `substrate.py`: `run_turn` is one REPL turn end to end, and the numbered comments inside it give the order things happen in. `agent.py` is the other entry, `substrate agent`. `parser.py` holds the parsed-record dataclass, the JSON schema the Claude parser is constrained to, the system prompt, and the rule-based parser. `version_store.py` turns model operations into path-level writes and defines the `VersionStore` interface with its two backends. `state.py` has the two settings, the focus pointer, the confirmation gate, and the check that keeps every write inside the sandbox. `generators.py` is four pure functions from model to text. `lexicon.py` and `trace.py` are small.

The Claude parser uses the Messages API with structured outputs (a JSON schema in `output_config`) and prompt caching on the system prompt. If structured output is rejected, it retries asking for plain JSON. If the call fails outright, it falls back to the rule-based parser for that turn and records why in the trace. Agent mode uses the same API with tool use. The system prompt is cached the same way. There is no rule-based stand-in: if the call fails, the loop stops, reports the error, and still writes whatever the graph holds.

## Agent mode

`substrate agent "goal"` does not run the one-sentence parser. It gives Claude tools and loops:

```mermaid
flowchart TD
    G["goal"] --> L["Claude tool call"]
    L --> P["submit_plan"]
    P --> A["act: add_node, add_edge, version tools, …"]
    A --> K{"audit_graph:<br/>orphans, missing edges,<br/>contradictions, implied nodes"}
    K -->|gaps remain| L
    K -->|clear, then finish| S["summary, uncertainties, questions"]
    K -->|20 steps| S
    S --> W["same generators and write policy as the REPL"]
```

The tools are the operations the REPL already applies, not a second model of the graph.

| Tool | What it does | Asks first? |
| --- | --- | --- |
| `inspect_graph`, `find_orphans`, `diff_versions`, `read_file` | Read the graph, orphans, a version diff, or a document | No |
| `add_node`, `update_node`, `add_edge`, `set_attr`, `add_tag`, `add_note`, `set_meta` | The ordinary edits. Each accepted edit commits a patch version | No, in every risk scope |
| `remove_node`, `remove_edge` | Deletions | Staging asks. Production refuses. Dev does not ask |
| `lock_in`, `walk_back`, `branch`, `merge` | Milestone bump, checkout, fork, merge | Staging and production. Dev does not ask |
| `switch` | Move to another branch | No |
| `submit_plan`, `finish` | The plan has to land before any edit. `finish` is rejected while checks fail | No |

`read_file` resolves a path against the workspace, `docs/` in the repo, and any `--docs` directory. A path that escapes those roots, a symlink that points out of them, or a secret name (`.env`, a key, a credential file) is refused. Nothing is written outside the sandbox; artifact files still go through `write_artifacts`, which asks on the `write` gate and only writes the formats the current intent mode allows.

The closing printout is always the same shape: a summary, the list of changes, open uncertainties, and questions. Exit status is 0 when the checks passed, 1 when they did not or the model call failed, and 2 when agent mode cannot start (no goal, `--dry-run`, no API key).

Intent mode, risk scope, and focus are whatever the workspace was last saved as. Agent mode does not coin terms and does not move those settings; say that in the REPL first if you need to. The lexicon is still injected into the opening message, so terms you have already coined are visible.

## Design decisions

**One structured graph, many disposable outputs.** The model in the version store is the only state that matters. Markdown, Mermaid and the Python stub are regenerated from it as pure string transforms, and the parser suggests which ones a given sentence calls for. Generating several at once costs nothing measurable, and there's no question of which artifact is authoritative. The cost is that the outputs are views, not editable sources: the Python stub says "do not edit", and hand edits to any artifact are overwritten on the next write.

**Two independent controls: intent mode and risk scope.** This is a trust-and-safety design for an agent that writes files. Intent mode (ideation, evaluation, execution) decides how eagerly output hits disk: ideation writes only JSON and previews the rest in the terminal, evaluation adds Markdown and Mermaid, and execution adds code. Risk scope (dev, staging, production) decides how contained the consequences are: dev never asks, staging and production confirm every write and structural change, and production refuses deletions outright. The two move independently, so you can explore freely in production or commit hard in dev. Separately, every artifact path is resolved and checked against the sandbox root before writing. The cost is two settings to keep in your head (the prompt shows both at all times), and the API parser can infer a mode change from your phrasing, which can surprise you.

**Versioning in plain language.** "Lock it in" seals a milestone and bumps the minor version, "walk it back to 0.0.2" makes an old snapshot the base again, "branch off that" forks, and "merge X back in" merges. Every sentence that changes the model also commits a patch version carrying the sentence itself, so provenance never has gaps. The cost is a lot of versions, and version numbers come from one global counter: after walking back to `v0.0.2`, the next commit is `v0.1.2`, not something that shows its lineage.

**Automerge for real branching and merging, behind an interface.** The default store is an Automerge document per branch: `branch` is a real fork and `merge` is a real CRDT merge. Writes are applied at node granularity, so concurrent edits to different nodes on two branches merge cleanly. The `VersionStore` interface also has an append-only JSON-log implementation with no native dependency, used with `--backend jsonlog` or automatically if Automerge fails to import. The cost is a pinned release candidate (`1.0.0rc1`), because the stable 0.1.2 needs a Rust toolchain and has no macOS wheel. The JSON-log backend's merge is last-write-wins per node, which is honest but not a CRDT.

**A vocabulary that starts empty.** Terms you coin are written to `substrate_workspace/lexicon.json` with the version and sentence they came from, and the whole lexicon is injected into the parser's context on every turn. A term coined at turn 3 changes how turn 4 is read. The repo ships no lexicon; it builds up per workspace. The cost is a prompt that grows with your vocabulary, and the offline parser records coinages but doesn't apply them.

**Inspectable execution.** Every turn is appended to `trace.jsonl`: the raw sentence, the full parsed record (including the parser's one-sentence rationale), the resulting version, the files written and any side effects. `show me the log`, `describe <version>` and `diff <a> <b>` read from it and from the store, so you can see where the capture drifted from what you meant. The fix is to walk back and say it again. The cost is a trace that grows without bound. There's also no way to edit a past parse in place. An agent run is one trace record with `source: agent`, and each edit is also its own version, so `describe` still shows the sentence (here, the tool summary) that produced it.

**The agent is checked by code, not by another prompt.** A goal is not one sentence, so stuffing it through the single-shot parser would hide the intermediate graph. The loop in `agent.py` lets the model plan and call tools, and `audit_graph` decides whether the result is connected and consistent. That check is deterministic: an orphan is a node with no edges once two nodes exist, a missing edge is either a dangling endpoint or a description that claims a relation the edge list does not have, a contradiction is a shared label, a self-edge, or opposing labels such as `feeds` and `not feeds`, and an implied node is one an edge created and nobody described. The cost is that the heuristics can nag (a description that mentions another unit and a relation verb has to grow an edge, or be reworded) and that a clean check is not a proof the goal was understood. The step cap is the escape hatch: leftover gaps are reported as open uncertainties instead of being papered over. Routine additions are not confirmed, including in staging, because a twenty-step loop that asked on every node would be unusable. Deletions, version-graph changes, and file writes still go through `SessionState.confirm` and `CONFIRM_POLICY`, and production still refuses deletions. In dev, that policy asks for nothing, same as the REPL.

## Limitations and known issues

- Input is typed text. The input path is a single `input()` call, so voice could replace it without touching anything else, but there's no voice input today.
- The offline parser is a handful of regexes. It needs literal phrasing, and it stores coined terms without applying them.
- A merge updates the branch's working model but doesn't create a new version, so `describe` and `diff` can't refer to the merge itself until the next change is committed.
- `describe` returns the stored sentence and the parser's note; it doesn't narrate what changed in prose.
- Automated tests cover the agent loop with a stubbed model, the confirmation gates, `read_file`'s sandbox, and a regression pass for the rule parser and the REPL (`python3 -m unittest discover -s tests`). They do not call the Anthropic API. The scripted demo above is still the way to see the REPL offline.
- The Claude model ID is a single constant in `config.py` (`MODEL_ID`), overridable with `--model`. The live parser and the live agent tool-use path were not exercised from this environment.
- Agent mode will not coin terms or change intent mode, risk scope, or focus. It will not run without an API key, and `--dry-run` only covers the REPL. A goal that needs a document outside the workspace, `docs/`, and `--docs` cannot be read. The graph check is a heuristic, and a run stops at 20 model calls even if the goal is unfinished.
- Out of scope for now: mobile, multi-user sync, remote execution.

## Project status

Personal project, paused. Last meaningful change: August 25, 2026.

## Built with

Python, the Anthropic Python SDK (Messages API with structured outputs and tool use), and Automerge (Python bindings). Designed by Matt Burke; built with Claude Code.

Related projects: [vsm-translator](https://github.com/more-reese/vsm-translator) (text and BPMN/VSM diagrams kept in sync) and [lenswork](https://github.com/more-reese/lenswork) (one case, eight reasoning lenses). lenswork used to live inside this repo and now has its own.

## License

MIT. See [LICENSE](LICENSE).
