"""The version graph as a first-class object.

`VersionStore` is the only thing in substrate that knows how versions are
stored. Two implementations satisfy it:

  * `AutomergeVersionStore` — the default. A real CRDT, so `branch` is a true
    fork and `merge` is a true merge, not a copy-and-hope.
  * `JsonLogVersionStore` — an append-only JSON log with snapshots, same
    interface, no native dependency.

Nothing outside this module knows which one is in use.

Version numbering: every content-bearing utterance commits a patch version
(v0.0.1, v0.0.2, ...) carrying the raw utterance that produced it, so provenance
is continuous. "Lock that in" seals a milestone and bumps the minor (v0.1.0).
Version ids are unique across branches; each version records the branch it was
made on.
"""

from __future__ import annotations

import copy
import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SEED_VERSION = "v0.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (text or "").strip().lower()).strip("_")
    return slug or "unnamed"


# ---------------------------------------------------------------------------
# The working model
# ---------------------------------------------------------------------------


def empty_model() -> dict:
    """The structured representation. This is the artifact; outputs are not."""
    return {
        "meta": {"name": "untitled", "summary": "", "created": _now()},
        "nodes": {},
        "edges": [],
        "notes": [],
    }


def ensure_shape(model: dict) -> dict:
    """Tolerate a model loaded from an older or partial workspace."""
    base = empty_model()
    for key, default in base.items():
        if not isinstance(model.get(key), type(default)):
            model[key] = default
    return model


@dataclass
class Write:
    """One path-level write. Both backends replay these identically."""

    kind: str  # "set" | "del" | "append"
    path: list  # e.g. ["nodes", "ignition"] — never deeper than 2
    value: Any = None


@dataclass
class OpResult:
    writes: list[Write] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


def _resolve_node_id(model: dict, op: dict) -> str:
    """Find the node an op refers to, by id then by label, else coin an id."""
    raw_id = (op.get("id") or "").strip()
    label = (op.get("label") or "").strip()
    if raw_id:
        candidate = _slug(raw_id)
        if candidate in model["nodes"]:
            return candidate
    if label:
        for nid, node in model["nodes"].items():
            if str(node.get("label", "")).strip().lower() == label.lower():
                return nid
    return _slug(raw_id or label or "node")


def ops_to_writes(
    model: dict,
    ops: Iterable[dict],
    focus_key: str = "root",
    allow_delete: bool = True,
) -> OpResult:
    """Translate parser model-ops into path-level writes.

    Pure: works on a copy of the model, so the caller can inspect the result
    (and the risk gate can veto it) before anything touches the store.
    """
    working = copy.deepcopy(ensure_shape(model))
    result = OpResult()

    def touch_node(nid: str) -> None:
        result.writes.append(Write("set", ["nodes", nid], working["nodes"][nid]))

    for op in ops or []:
        if not isinstance(op, dict):
            continue
        kind = (op.get("op") or "").strip()

        if kind == "add_node" or kind == "update_node":
            nid = _resolve_node_id(working, op)
            node = working["nodes"].get(nid) or {
                "id": nid,
                "label": "",
                "kind": "",
                "description": "",
                "attrs": {},
                "tags": [],
                "focus": focus_key,
            }
            existed = nid in working["nodes"]
            for src_key, dst_key in (
                ("label", "label"),
                ("kind", "kind"),
                ("description", "description"),
            ):
                value = (op.get(src_key) or "").strip()
                if value:
                    node[dst_key] = value
            if not node.get("label"):
                node["label"] = (op.get("id") or nid).replace("_", " ")
            working["nodes"][nid] = node
            touch_node(nid)
            verb = "updated" if existed else "added"
            result.summaries.append(f"{verb} node {nid} ({node.get('kind') or 'node'})")

        elif kind == "remove_node":
            nid = _resolve_node_id(working, op)
            if nid not in working["nodes"]:
                result.refusals.append(f"no such node to remove: {nid}")
                continue
            if not allow_delete:
                result.refusals.append(
                    f"refused to remove node {nid} — deletion is not permitted in this risk scope"
                )
                continue
            del working["nodes"][nid]
            result.writes.append(Write("del", ["nodes", nid]))
            remaining = [
                e for e in working["edges"] if e.get("src") != nid and e.get("dst") != nid
            ]
            if len(remaining) != len(working["edges"]):
                working["edges"] = remaining
                result.writes.append(Write("set", ["edges"], remaining))
            result.summaries.append(f"removed node {nid} and its edges")

        elif kind == "add_edge":
            src = _resolve_node_id(working, {"id": op.get("src")})
            dst = _resolve_node_id(working, {"id": op.get("dst")})
            if not src or not dst:
                result.refusals.append("edge needs both a source and a target")
                continue
            for endpoint in (src, dst):
                if endpoint not in working["nodes"]:
                    working["nodes"][endpoint] = {
                        "id": endpoint,
                        "label": endpoint.replace("_", " "),
                        "kind": "implied",
                        "description": "implied by an edge before it was described",
                        "attrs": {},
                        "tags": [],
                        "focus": focus_key,
                    }
                    touch_node(endpoint)
                    result.summaries.append(f"implied node {endpoint} from an edge")
            edge = {
                "src": src,
                "dst": dst,
                "label": (op.get("label") or "").strip(),
            }
            if edge in working["edges"]:
                continue
            working["edges"].append(edge)
            result.writes.append(Write("append", ["edges"], edge))
            result.summaries.append(
                f"linked {src} → {dst}" + (f" ({edge['label']})" if edge["label"] else "")
            )

        elif kind == "remove_edge":
            src = _slug(op.get("src") or "")
            dst = _slug(op.get("dst") or "")
            if not allow_delete:
                result.refusals.append(
                    f"refused to remove edge {src} → {dst} — deletion is not permitted in this risk scope"
                )
                continue
            remaining = [
                e
                for e in working["edges"]
                if not (e.get("src") == src and e.get("dst") == dst)
            ]
            if len(remaining) == len(working["edges"]):
                result.refusals.append(f"no such edge to remove: {src} → {dst}")
                continue
            working["edges"] = remaining
            result.writes.append(Write("set", ["edges"], remaining))
            result.summaries.append(f"removed edge {src} → {dst}")

        elif kind == "set_attr":
            nid = _resolve_node_id(working, op)
            key = (op.get("key") or "").strip()
            if not key:
                result.refusals.append("set_attr needs a key")
                continue
            node = working["nodes"].get(nid)
            if node is None:
                result.refusals.append(f"no such node for set_attr: {nid}")
                continue
            node.setdefault("attrs", {})[key] = (op.get("value") or "").strip()
            touch_node(nid)
            result.summaries.append(f"set {nid}.{key}")

        elif kind == "add_tag":
            nid = _resolve_node_id(working, op)
            tag = (op.get("value") or op.get("key") or "").strip()
            node = working["nodes"].get(nid)
            if node is None or not tag:
                result.refusals.append(f"could not tag {nid}")
                continue
            tags = node.setdefault("tags", [])
            if tag not in tags:
                tags.append(tag)
                touch_node(nid)
                result.summaries.append(f"tagged {nid} as {tag}")

        elif kind == "add_note":
            text = (op.get("text") or op.get("value") or "").strip()
            if not text:
                continue
            working["notes"].append(text)
            result.writes.append(Write("append", ["notes"], text))
            result.summaries.append("recorded a note")

        elif kind == "set_meta":
            key = (op.get("key") or "").strip()
            value = (op.get("value") or "").strip()
            if not key:
                continue
            working["meta"][key] = value
            result.writes.append(Write("set", ["meta", key], value))
            result.summaries.append(f"set meta.{key}")

        else:
            result.refusals.append(f"unrecognised model op: {kind or '(blank)'}")

    return result


def apply_writes(model: dict, writes: Iterable[Write]) -> dict:
    """Replay writes onto a plain dict (the JSON-log backend's storage)."""
    ensure_shape(model)
    for write in writes:
        container = model
        for segment in write.path[:-1]:
            container = container.setdefault(segment, {})
        leaf = write.path[-1]
        if write.kind == "set":
            container[leaf] = copy.deepcopy(write.value)
        elif write.kind == "del":
            container.pop(leaf, None)
        elif write.kind == "append":
            container.setdefault(leaf, []).append(copy.deepcopy(write.value))
    return model


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts to dotted paths; lists are compared as multisets."""
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        flat[prefix] = [json.dumps(v, sort_keys=True) for v in value]
    else:
        flat[prefix] = value
    return flat


def structural_diff(before: dict, after: dict) -> list[dict]:
    """A path-level diff of two model snapshots, legible to a human."""
    flat_a = _flatten(before or {})
    flat_b = _flatten(after or {})
    changes: list[dict] = []

    for path in sorted(set(flat_a) | set(flat_b)):
        old, new = flat_a.get(path, KeyError), flat_b.get(path, KeyError)
        if old is KeyError:
            changes.append({"op": "added", "path": path, "before": None, "after": new})
        elif new is KeyError:
            changes.append({"op": "removed", "path": path, "before": old, "after": None})
        elif isinstance(old, list) and isinstance(new, list):
            gained = [json.loads(x) for x in new if x not in old]
            lost = [json.loads(x) for x in old if x not in new]
            for item in gained:
                changes.append(
                    {"op": "added", "path": f"{path}[]", "before": None, "after": item}
                )
            for item in lost:
                changes.append(
                    {"op": "removed", "path": f"{path}[]", "before": item, "after": None}
                )
        elif old != new:
            changes.append(
                {"op": "changed", "path": path, "before": old, "after": new}
            )
    return changes


# ---------------------------------------------------------------------------
# Version metadata
# ---------------------------------------------------------------------------


@dataclass
class VersionInfo:
    id: str
    branch: str
    parent: str | None
    raw: str
    notes: str
    mode: str
    scope: str
    focus: str
    ts: str
    milestone: bool = False
    heads: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VersionInfo":
        return cls(
            id=data.get("id", "?"),
            branch=data.get("branch", "main"),
            parent=data.get("parent"),
            raw=data.get("raw", ""),
            notes=data.get("notes", ""),
            mode=data.get("mode", ""),
            scope=data.get("scope", ""),
            focus=data.get("focus", "root"),
            ts=data.get("ts", ""),
            milestone=bool(data.get("milestone", False)),
            heads=list(data.get("heads", [])),
        )


class VersionNotFound(KeyError):
    pass


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class VersionStore(ABC):
    """Every version operation in substrate goes through this."""

    backend_name = "abstract"

    # -- lifecycle
    @abstractmethod
    def init(self) -> None: ...

    # -- reading
    @abstractmethod
    def current(self) -> dict:
        """The working model right now (including uncommitted writes)."""

    @abstractmethod
    def current_version(self) -> str: ...

    @abstractmethod
    def current_branch(self) -> str: ...

    @abstractmethod
    def model_at(self, version_id: str) -> dict: ...

    @abstractmethod
    def list_versions(self, branch: str | None = None) -> list[VersionInfo]: ...

    @abstractmethod
    def get_version(self, version_id: str) -> VersionInfo: ...

    @abstractmethod
    def branches(self) -> list[str]: ...

    # -- writing
    @abstractmethod
    def update(self, writes: Iterable[Write]) -> None:
        """Merge writes into the working model, without sealing a version."""

    @abstractmethod
    def commit(
        self,
        raw: str,
        notes: str,
        mode: str,
        scope: str,
        focus: str,
        milestone: bool = False,
    ) -> VersionInfo:
        """Seal the working model as a new version. This is "lock it in"."""

    @abstractmethod
    def checkout(self, version_id: str) -> VersionInfo:
        """Walk back: make an earlier version the base for what comes next."""

    @abstractmethod
    def branch(self, name: str) -> str: ...

    @abstractmethod
    def merge(self, name: str) -> list[dict]:
        """Merge another branch into the current one; returns the diff applied."""

    def diff(self, a: str, b: str) -> dict:
        """Shared: both backends diff by comparing two committed snapshots."""
        model_a, model_b = self.model_at(a), self.model_at(b)
        return {
            "a": a,
            "b": b,
            "changes": structural_diff(model_a, model_b),
            "detail": None,
        }


# ---------------------------------------------------------------------------
# Shared registry bookkeeping
# ---------------------------------------------------------------------------


class _RegistryMixin:
    """Version numbering, branch bookkeeping, and registry persistence."""

    def __init__(self, root: Path):
        self.root = root
        self.branches_dir = root / "branches"
        self.snapshots_dir = root / "snapshots"
        self.registry_path = root / "registry.json"
        self.registry: dict = {}

    def _load_registry(self) -> None:
        if self.registry_path.exists():
            try:
                self.registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
                return
            except (json.JSONDecodeError, OSError):
                pass
        self.registry = {
            "backend": self.backend_name,
            "counter": {"major": 0, "minor": 0, "patch": 0},
            "current_branch": "main",
            "current_version": SEED_VERSION,
            "branches": {"main": {"created_from": None, "head": SEED_VERSION}},
            "versions": [],
        }

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(self.registry, indent=2), encoding="utf-8"
        )

    def _next_version_id(self, milestone: bool) -> str:
        counter = self.registry["counter"]
        if milestone:
            counter["minor"] += 1
            counter["patch"] = 0
        else:
            counter["patch"] += 1
        return f"v{counter['major']}.{counter['minor']}.{counter['patch']}"

    def current_version(self) -> str:
        return self.registry.get("current_version", SEED_VERSION)

    def current_branch(self) -> str:
        return self.registry.get("current_branch", "main")

    def branches(self) -> list[str]:
        return sorted(self.registry.get("branches", {}))

    def list_versions(self, branch: str | None = None) -> list[VersionInfo]:
        versions = [VersionInfo.from_dict(v) for v in self.registry.get("versions", [])]
        if branch:
            versions = [v for v in versions if v.branch == branch]
        return versions

    def get_version(self, version_id: str) -> VersionInfo:
        target = normalise_version_id(version_id)
        for entry in self.registry.get("versions", []):
            if entry.get("id") == target:
                return VersionInfo.from_dict(entry)
        raise VersionNotFound(version_id)

    def _record(self, info: VersionInfo) -> None:
        self.registry["versions"].append(info.to_dict())
        self.registry["current_version"] = info.id
        self.registry["branches"].setdefault(
            info.branch, {"created_from": None, "head": info.id}
        )["head"] = info.id
        self._save_registry()


def normalise_version_id(text: str) -> str:
    """Accept "0.0.5", "v0.0.5", "version 0.0.5", "v3" and friends."""
    text = (text or "").strip().lower()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match:
        return "v{}.{}.{}".format(*match.groups())
    match = re.search(r"(\d+)\.(\d+)", text)
    if match:
        return "v0.{}.{}".format(*match.groups())
    match = re.search(r"(\d+)", text)
    if match:
        return f"v0.0.{match.group(1)}"
    return text


# ---------------------------------------------------------------------------
# Automerge implementation (default)
# ---------------------------------------------------------------------------


class AutomergeVersionStore(_RegistryMixin, VersionStore):
    """Backed by Automerge: a genuine CRDT with real fork and real merge."""

    backend_name = "automerge"

    def __init__(self, root: Path):
        _RegistryMixin.__init__(self, root)
        from automerge import Document, core  # imported lazily so --backend works
        from automerge.document import MapReadProxy

        self._Document = Document
        self._core = core
        self._MapReadProxy = MapReadProxy
        self._doc = None

    # -- automerge plumbing -------------------------------------------------

    def _wrap(self, core_doc):
        """Put the ergonomic proxy back around a raw core document."""
        doc = self._Document.__new__(self._Document)
        doc._doc = core_doc
        self._MapReadProxy.__init__(doc, core_doc, self._core.ROOT, None)
        return doc

    def _branch_path(self, branch: str) -> Path:
        return self.branches_dir / f"{_slug(branch)}.amrg"

    def _snapshot_path(self, version_id: str) -> Path:
        return self.snapshots_dir / f"{version_id}.amrg"

    def _load_branch(self, branch: str):
        path = self._branch_path(branch)
        if path.exists():
            return self._wrap(self._core.Document.load(path.read_bytes()))
        doc = self._Document()
        with doc.change() as model:
            for key, value in empty_model().items():
                model[key] = value
        return doc

    def _save_branch(self) -> None:
        self.branches_dir.mkdir(parents=True, exist_ok=True)
        self._branch_path(self.current_branch()).write_bytes(self._doc._doc.save())

    def _heads_hex(self) -> list[str]:
        return [h.hex() for h in self._doc._doc.get_heads()]

    # -- interface ----------------------------------------------------------

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.branches_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._load_registry()
        self._doc = self._load_branch(self.current_branch())
        self._save_branch()
        if not self.registry["versions"]:
            seed = VersionInfo(
                id=SEED_VERSION,
                branch="main",
                parent=None,
                raw="(session seeded)",
                notes="empty model created at first run",
                mode="",
                scope="",
                focus="root",
                ts=_now(),
                heads=self._heads_hex(),
            )
            self._snapshot_path(SEED_VERSION).write_bytes(self._doc._doc.save())
            self._record(seed)

    def current(self) -> dict:
        return ensure_shape(self._doc.to_py())

    def model_at(self, version_id: str) -> dict:
        vid = normalise_version_id(version_id)
        path = self._snapshot_path(vid)
        if not path.exists():
            raise VersionNotFound(version_id)
        return ensure_shape(
            self._core.extract(self._core.Document.load(path.read_bytes()), self._core.ROOT)
        )

    def update(self, writes: Iterable[Write]) -> None:
        writes = list(writes)
        if not writes:
            return
        with self._doc.change() as model:
            for write in writes:
                container = model
                for segment in write.path[:-1]:
                    container = container[segment]
                leaf = write.path[-1]
                if write.kind == "set":
                    container[leaf] = write.value
                elif write.kind == "del":
                    if leaf in container:
                        del container[leaf]
                elif write.kind == "append":
                    container[leaf].append(write.value)
        self._save_branch()

    def commit(
        self,
        raw: str,
        notes: str,
        mode: str,
        scope: str,
        focus: str,
        milestone: bool = False,
    ) -> VersionInfo:
        version_id = self._next_version_id(milestone)
        self._snapshot_path(version_id).write_bytes(self._doc._doc.save())
        info = VersionInfo(
            id=version_id,
            branch=self.current_branch(),
            parent=self.current_version(),
            raw=raw,
            notes=notes,
            mode=mode,
            scope=scope,
            focus=focus,
            ts=_now(),
            milestone=milestone,
            heads=self._heads_hex(),
        )
        self._record(info)
        self._save_branch()
        return info

    def checkout(self, version_id: str) -> VersionInfo:
        info = self.get_version(version_id)
        path = self._snapshot_path(info.id)
        if not path.exists():
            raise VersionNotFound(version_id)
        self._doc = self._wrap(self._core.Document.load(path.read_bytes()))
        self.registry["current_version"] = info.id
        self._save_branch()
        self._save_registry()
        return info

    def branch(self, name: str) -> str:
        branch_name = _slug(name)
        if branch_name in self.registry["branches"]:
            raise ValueError(f"branch already exists: {branch_name}")
        forked = self._wrap(self._doc._doc.fork())
        forked._doc.set_actor(self._core.random_actor_id())
        self.registry["branches"][branch_name] = {
            "created_from": self.current_branch(),
            "forked_at": self.current_version(),
            "head": self.current_version(),
        }
        self.registry["current_branch"] = branch_name
        self._doc = forked
        self._save_branch()
        self._save_registry()
        return branch_name

    def merge(self, name: str) -> list[dict]:
        branch_name = _slug(name)
        if branch_name not in self.registry["branches"]:
            raise ValueError(f"no such branch: {branch_name}")
        if branch_name == self.current_branch():
            raise ValueError("cannot merge a branch into itself")
        before = self.current()
        other = self._load_branch(branch_name)
        self._doc._doc.merge(other._doc)
        self._save_branch()
        return structural_diff(before, self.current())

    def switch(self, name: str) -> str:
        branch_name = _slug(name)
        if branch_name not in self.registry["branches"]:
            raise ValueError(f"no such branch: {branch_name}")
        self._doc = self._load_branch(branch_name)
        self.registry["current_branch"] = branch_name
        self.registry["current_version"] = self.registry["branches"][branch_name].get(
            "head", self.current_version()
        )
        self._save_registry()
        return branch_name

    def diff(self, a: str, b: str) -> dict:
        result = super().diff(a, b)
        # Supplement the readable diff with the CRDT's own view of the change.
        try:
            info_a, info_b = self.get_version(a), self.get_version(b)
            heads_a = [bytes.fromhex(h) for h in info_a.heads]
            heads_b = [bytes.fromhex(h) for h in info_b.heads]
            patches = self._doc._doc.diff(heads_a, heads_b)
            result["detail"] = f"{len(patches)} automerge patch(es) between these heads"
        except Exception:
            result["detail"] = None
        return result


# ---------------------------------------------------------------------------
# Append-only JSON log implementation (fallback)
# ---------------------------------------------------------------------------


class JsonLogVersionStore(_RegistryMixin, VersionStore):
    """Same interface, no native dependency: an append-only log + snapshots."""

    backend_name = "jsonlog"

    def __init__(self, root: Path):
        _RegistryMixin.__init__(self, root)
        self.log_path = root / "log.jsonl"
        self._model: dict = empty_model()

    def _branch_path(self, branch: str) -> Path:
        return self.branches_dir / f"{_slug(branch)}.json"

    def _snapshot_path(self, version_id: str) -> Path:
        return self.snapshots_dir / f"{version_id}.json"

    def _append_log(self, event: str, payload: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _now(), "event": event, "branch": self.current_branch(), **payload}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _save_branch(self) -> None:
        self.branches_dir.mkdir(parents=True, exist_ok=True)
        self._branch_path(self.current_branch()).write_text(
            json.dumps(self._model, indent=2), encoding="utf-8"
        )

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.branches_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._load_registry()
        path = self._branch_path(self.current_branch())
        if path.exists():
            try:
                self._model = ensure_shape(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                self._model = empty_model()
        else:
            self._model = empty_model()
        self._save_branch()
        if not self.registry["versions"]:
            self._snapshot_path(SEED_VERSION).write_text(
                json.dumps(self._model, indent=2), encoding="utf-8"
            )
            self._append_log("init", {"version": SEED_VERSION})
            self._record(
                VersionInfo(
                    id=SEED_VERSION,
                    branch="main",
                    parent=None,
                    raw="(session seeded)",
                    notes="empty model created at first run",
                    mode="",
                    scope="",
                    focus="root",
                    ts=_now(),
                )
            )

    def current(self) -> dict:
        return copy.deepcopy(ensure_shape(self._model))

    def model_at(self, version_id: str) -> dict:
        vid = normalise_version_id(version_id)
        path = self._snapshot_path(vid)
        if not path.exists():
            raise VersionNotFound(version_id)
        return ensure_shape(json.loads(path.read_text(encoding="utf-8")))

    def update(self, writes: Iterable[Write]) -> None:
        writes = list(writes)
        if not writes:
            return
        apply_writes(self._model, writes)
        self._append_log(
            "update",
            {"writes": [{"kind": w.kind, "path": w.path, "value": w.value} for w in writes]},
        )
        self._save_branch()

    def commit(
        self,
        raw: str,
        notes: str,
        mode: str,
        scope: str,
        focus: str,
        milestone: bool = False,
    ) -> VersionInfo:
        version_id = self._next_version_id(milestone)
        self._snapshot_path(version_id).write_text(
            json.dumps(self._model, indent=2), encoding="utf-8"
        )
        info = VersionInfo(
            id=version_id,
            branch=self.current_branch(),
            parent=self.current_version(),
            raw=raw,
            notes=notes,
            mode=mode,
            scope=scope,
            focus=focus,
            ts=_now(),
            milestone=milestone,
        )
        self._append_log("commit", {"version": version_id, "raw": raw, "milestone": milestone})
        self._record(info)
        self._save_branch()
        return info

    def checkout(self, version_id: str) -> VersionInfo:
        info = self.get_version(version_id)
        self._model = self.model_at(info.id)
        self.registry["current_version"] = info.id
        self._append_log("checkout", {"version": info.id})
        self._save_branch()
        self._save_registry()
        return info

    def branch(self, name: str) -> str:
        branch_name = _slug(name)
        if branch_name in self.registry["branches"]:
            raise ValueError(f"branch already exists: {branch_name}")
        self.registry["branches"][branch_name] = {
            "created_from": self.current_branch(),
            "forked_at": self.current_version(),
            "head": self.current_version(),
        }
        self._append_log("branch", {"name": branch_name, "from": self.current_branch()})
        self.registry["current_branch"] = branch_name
        self._save_branch()
        self._save_registry()
        return branch_name

    def merge(self, name: str) -> list[dict]:
        branch_name = _slug(name)
        if branch_name not in self.registry["branches"]:
            raise ValueError(f"no such branch: {branch_name}")
        if branch_name == self.current_branch():
            raise ValueError("cannot merge a branch into itself")
        path = self._branch_path(branch_name)
        if not path.exists():
            raise ValueError(f"branch has no stored model: {branch_name}")
        before = self.current()
        other = ensure_shape(json.loads(path.read_text(encoding="utf-8")))
        # Last-write-wins per node/key — the honest limit of a non-CRDT backend.
        self._model["nodes"].update(other["nodes"])
        for edge in other["edges"]:
            if edge not in self._model["edges"]:
                self._model["edges"].append(edge)
        for note in other["notes"]:
            if note not in self._model["notes"]:
                self._model["notes"].append(note)
        self._model["meta"].update(other["meta"])
        self._append_log("merge", {"from": branch_name})
        self._save_branch()
        return structural_diff(before, self.current())

    def switch(self, name: str) -> str:
        branch_name = _slug(name)
        if branch_name not in self.registry["branches"]:
            raise ValueError(f"no such branch: {branch_name}")
        path = self._branch_path(branch_name)
        self._model = (
            ensure_shape(json.loads(path.read_text(encoding="utf-8")))
            if path.exists()
            else empty_model()
        )
        self.registry["current_branch"] = branch_name
        self.registry["current_version"] = self.registry["branches"][branch_name].get(
            "head", self.current_version()
        )
        self._save_registry()
        return branch_name


# ---------------------------------------------------------------------------


def make_store(backend: str, root: Path) -> VersionStore:
    """Build the configured store. Automerge is the default; jsonlog is the
    fallback, and is also used automatically if the Automerge binding is
    missing so the app still runs."""
    if backend == "jsonlog":
        return JsonLogVersionStore(root)
    try:
        return AutomergeVersionStore(root)
    except ImportError as exc:
        print(f"  ! automerge unavailable ({exc}); falling back to the JSON-log store.")
        return JsonLogVersionStore(root)
