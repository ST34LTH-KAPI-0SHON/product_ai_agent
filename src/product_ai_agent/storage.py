"""Confined paths, atomic files and recoverable multi-file operations."""
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .models import IDENTIFIER, Policy, State


class AgentError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def fail(code, message):
    raise AgentError(code, message)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(value):
    if not re.fullmatch(IDENTIFIER, value):
        fail("INVALID_INPUT", "ID must contain only letters, digits, hyphens and underscores (max 96).")
    return value


def no_links(path):
    for part in [path, *path.parents]:
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            fail("ACCESS_DENIED", f"Symlink/junction is not allowed: {part.name}")


def project_path(value, workspace=None):
    workspace = Path(workspace or Path.cwd()).resolve()
    raw = Path(value)
    raw = raw if raw.is_absolute() else workspace / raw
    no_links(raw)
    resolved = raw.resolve()
    if not resolved.is_relative_to(workspace) or resolved == workspace:
        fail("ACCESS_DENIED", "Project must be a directory strictly inside the current workspace.")
    return resolved


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    no_links(path)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        no_links(self.root)

    def path(self, value):
        if not isinstance(value, str) or "\\" in value or ":" in value:
            fail("ACCESS_DENIED", "Use project-relative paths with forward slashes.")
        rel = PurePosixPath(value)
        if not value or rel.is_absolute() or ".." in rel.parts:
            fail("ACCESS_DENIED", "Path escapes the project.")
        target = self.root.joinpath(*rel.parts)
        no_links(target)
        if not target.resolve().is_relative_to(self.root):
            fail("ACCESS_DENIED", "Path escapes the project.")
        return target

    def read(self, name):
        path = self.path(name)
        if not path.is_file():
            fail("INVALID_INPUT", f"Missing project file: {name}")
        if path.stat().st_size > 40 * 1024 * 1024:
            fail("LIMIT_EXCEEDED", f"File too large: {name}")
        return path.read_bytes()

    def json(self, name):
        try:
            return json.loads(self.read(name).decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            fail("PARSE_ERROR", f"Invalid JSON in {name}: {exc}")

    def registry(self, name):
        try:
            rows = [json.loads(line) for line in self.read(name).decode("utf-8").splitlines() if line.strip()]
        except (ValueError, UnicodeError) as exc:
            fail("PARSE_ERROR", f"Invalid registry {name}: {exc}")
        key = "event_id" if name == "operations.jsonl" else "id"
        ids = [r.get(key) for r in rows]
        if any(not i for i in ids) or len(ids) != len(set(ids)):
            fail("VALIDATION_ERROR", f"Missing/duplicate {key} in {name}")
        return rows

    def state(self):
        return State.model_validate(self.json("state.json")).model_dump()

    def policy(self):
        return Policy.model_validate(self.json("policy.json")).model_dump()

    def source(self, source_id):
        for record in self.registry("sources.jsonl"):
            if record["id"] == source_id:
                for path, hash_key in [("snapshot_path", "sha256"), ("extracted_path", "extracted_sha256")]:
                    if digest(self.read(record[path])) != record[hash_key]:
                        fail("VALIDATION_ERROR", f"Source checksum mismatch: {source_id}/{path}")
                return record
        fail("INVALID_INPUT", f"Unknown source: {source_id}")

    @contextmanager
    def lock(self):
        path = self.path(".writer.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            fail("INCOMPLETE_OPERATION", "Writer lock exists. Check no process is writing before removing .writer.lock.")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            yield
        finally:
            path.unlink(missing_ok=True)

    def event(self, op, status, **fields):
        rows = self.registry("operations.jsonl")
        rows.append({"event_id": uuid.uuid4().hex, "operation_id": op, "status": status,
                     "at": now(), "cost": None, **fields})
        atomic(self.path("operations.jsonl"), registry_bytes(rows))

    def pending(self):
        latest = {}
        for event in self.registry("operations.jsonl"):
            latest[event["operation_id"]] = event
        return [v for v in latest.values() if v["status"] == "started"]

    def apply_manifest(self, manifest):
        for item in manifest["writes"]:
            payload = self.read(item["staged"])
            if digest(payload) != item["sha256"]:
                fail("VALIDATION_ERROR", "Staged operation payload is corrupt.")
            self.path(item["target"])
        for item in manifest["writes"]:
            atomic(self.path(item["target"]), self.read(item["staged"]))

    def mutate(self, op, command, params, action):
        identifier(op)
        fingerprint = digest(encode({"command": command, "params": params}))
        with self.lock():
            events = [r for r in self.registry("operations.jsonl") if r["operation_id"] == op]
            if events and events[0]["input_hash"] != fingerprint:
                fail("REVISION_CONFLICT", "Operation ID was already used with different inputs.")
            if events and events[-1]["status"] == "succeeded":
                manifest = self.json(events[-1]["manifest_path"])
                # A receipt is not proof that the current outputs still exist.
                for item in manifest["writes"]:
                    if not self.path(item["target"]).is_file():
                        fail("VALIDATION_ERROR", f"Operation output missing: {item['target']}")
                    if item["target"].startswith(("sources/", "calculations/")) and digest(self.read(item["target"])) != item["sha256"]:
                        fail("VALIDATION_ERROR", f"Immutable output changed: {item['target']}")
                return events[-1]["result"]
            if self.pending():
                fail("INCOMPLETE_OPERATION", "Unfinished operation; inspect status and run recover before writing.")
            start = time.monotonic()
            self.event(op, "started", input_hash=fingerprint, command=command)
            manifest_path = f".operations/{op}/{uuid.uuid4().hex}/manifest.json"
            try:
                data, writes, warnings = action()
                items = []
                parent = str(PurePosixPath(manifest_path).parent)
                for index, (target, payload) in enumerate(writes.items()):
                    if target == "operations.jsonl" or target.startswith(".operations/"):
                        fail("ACCESS_DENIED", "Operation cannot replace its journal.")
                    self.path(target)
                    staged = f"{parent}/{index}.bin"
                    atomic(self.path(staged), payload)
                    items.append({"target": target, "staged": staged, "sha256": digest(payload)})
                result = {"data": data, "warnings": warnings}
                manifest = {"operation_id": op, "input_hash": fingerprint, "writes": items, "result": result}
                atomic(self.path(manifest_path), encode(manifest))
                self.apply_manifest(manifest)
                self.event(op, "succeeded", input_hash=fingerprint, result=result,
                           manifest_path=manifest_path, duration_seconds=time.monotonic() - start)
                return result
            except Exception as exc:
                if self.path(manifest_path).exists():
                    # Keep started status: a prepared transaction must be finished explicitly.
                    raise AgentError("INCOMPLETE_OPERATION", f"Prepared operation interrupted; run recover for {op}.") from exc
                self.event(op, "failed", input_hash=fingerprint, error=type(exc).__name__,
                           duration_seconds=time.monotonic() - start)
                raise

    def recover(self, op, action):
        identifier(op)
        with self.lock():
            pending = next((e for e in self.pending() if e["operation_id"] == op), None)
            if not pending:
                fail("INVALID_INPUT", "No pending operation with that ID.")
            candidates = list(self.path(f".operations/{op}").glob("*/manifest.json"))
            manifests = [(p, self.json(p.relative_to(self.root).as_posix())) for p in candidates]
            manifests = [(p, m) for p, m in manifests if m["input_hash"] == pending["input_hash"]]
            if action == "finish":
                if len(manifests) != 1:
                    fail("INCOMPLETE_OPERATION", "No unique prepared manifest; unprepared operation can be aborted.")
                path, manifest = manifests[0]
                self.apply_manifest(manifest)
                self.event(op, "succeeded", input_hash=pending["input_hash"], result=manifest["result"],
                           manifest_path=path.relative_to(self.root).as_posix(), recovered=True)
                return manifest["result"]
            if manifests:
                fail("INCOMPLETE_OPERATION", "Prepared operation may have outputs; use finish, not abort.")
            self.event(op, "interrupted", input_hash=pending["input_hash"], recovered=True)
            return {"data": {"operation_id": op, "aborted": True}, "warnings": []}


def registry_bytes(rows):
    return b"".join((json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") for r in rows)


def init_project(root, op):
    identifier(op)
    identifier(root.name)
    if root.exists():
        store = Store(root)
        if not store.path("state.json").exists():
            fail("INCOMPLETE_OPERATION", "Project directory exists without complete state; refusing overwrite.")
        state = store.state()
        events = store.registry("operations.jsonl")
        if any(e["operation_id"] == op and e.get("command") == "init" for e in events):
            return {"data": {"project_id": state["project_id"], "path": root.name}, "warnings": []}
        fail("INVALID_INPUT", "Project already exists; use status or resume.")
    # Publish the complete initial directory in one rename on the same filesystem.
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".init-", dir=root.parent))
    store = Store(staging)
    for name in ["inputs", "sources", "calculations", "checkpoints", "reports", "drafts", ".operations"]:
        store.path(name).mkdir()
    state = State(project_id=root.name, revision=0, stage="brief", status="running", completed_stages=[],
                  next_action="Read instructions/entry.md and fill the brief from the user request.",
                  open_questions=[], artifacts={}, pending_decision=None,
                  last_completed_operation_id=op, updated_at=now())
    atomic(store.path("policy.json"), encode(Policy().model_dump()))
    for registry in ["sources", "claims", "decisions", "operations"]:
        atomic(store.path(f"{registry}.jsonl"), b"")
    atomic(store.path("state.json"), encode(state.model_dump()))
    store.event(op, "succeeded", command="init", input_hash=digest(encode({"project_id": root.name})))
    os.rename(staging, root)
    return {"data": {"project_id": root.name, "path": root.name}, "warnings": []}
