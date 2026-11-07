"""One JSON response per invocation; help is the only human-readable output."""
import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .analytics import aggregate, profile
from .ingestion import ingest_file
from .models import Policy
from .storage import AgentError, Store, digest, encode, fail, init_project, project_path
from .web import canonical, ingest_url
from .workflow import checkpoint, record_claim, record_decision, resume, retract_claim, validation


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentError("INVALID_INPUT", message)


def parser():
    root = Parser(description="Product research tools. The host environment supplies the LLM.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    mutations = {"init", "ingest-file", "ingest-url", "aggregate", "record-claim", "record-decision",
                 "checkpoint", "configure", "write-artifact", "retract-claim"}
    for command in ["init", "status", "resume", "validate", "ingest-file", "ingest-url", "profile-table",
                    "aggregate", "record-claim", "record-decision", "checkpoint", "configure", "write-artifact",
                    "recover", "retract-claim"]:
        sub = commands.add_parser(command)
        sub.add_argument("--project", required=True)
        if command in mutations or command == "recover":
            sub.add_argument("--operation-id", required=True)
        if command == "ingest-file":
            sub.add_argument("--file", required=True)
            sub.add_argument("--delimiter", default=None)
            sub.add_argument("--encoding", default="utf-8-sig")
            sub.add_argument("--provenance", choices=["user_provided", "synthetic", "public"], default="user_provided")
        if command == "ingest-url":
            sub.add_argument("--url", required=True)
        if command in {"aggregate", "profile-table"}:
            sub.add_argument("--source", required=True)
        if command == "profile-table":
            sub.add_argument("--sheet")
            sub.add_argument("--date-column")
            # Accepted for compatibility with the original CLI examples; no journal mutation.
            sub.add_argument("--operation-id")
        if command == "aggregate":
            sub.add_argument("--spec", required=True)
        if command in {"record-claim", "record-decision", "checkpoint", "configure", "write-artifact"}:
            sub.add_argument("--input", required=True)
        if command == "checkpoint":
            sub.add_argument("--expected-revision", type=int, required=True)
        if command == "write-artifact":
            sub.add_argument("--path", required=True)
        if command == "recover":
            sub.add_argument("--action", choices=["finish", "abort"], required=True)
        if command == "retract-claim":
            sub.add_argument("--claim", required=True)
            sub.add_argument("--reason", required=True)
    for command in ["start", "session", "save", "choose", "report", "doctor", "export", "pause", "impact", "quality", "now", "track", "versions", "changes", "expand"]:
        sub = commands.add_parser(command)
        sub.add_argument("--project")
        if command == "start":
            from .routing import MODES
            sub.add_argument("--mode", choices=MODES, default="strategy")
            sub.add_argument("--quantitative", action="store_true")
            sub.add_argument("--routing-reason")
            sub.add_argument("--depth", choices=["quick", "full"])
            sub.add_argument("--title", default="Новое исследование")
            sub.add_argument("--question")
            sub.add_argument("--material", action="append", default=[])
            sub.add_argument("--allow-url", action="append", default=[])
            sub.add_argument("--synthetic", action="store_true")
            sub.add_argument("--demo", action="store_true")
        if command == "save":
            sub.add_argument("--input", required=True)
        if command == "track":
            sub.add_argument("--input", required=True)
        if command == "changes":
            sub.add_argument("--from", dest="from_version")
            sub.add_argument("--to", dest="to_version")
        if command == "expand":
            from .routing import MODES
            sub.add_argument("--mode", choices=MODES)
            sub.add_argument("--reason", required=True)
        if command == "choose":
            sub.add_argument("--option", required=True)
            sub.add_argument("--quote", required=True)
            sub.add_argument("--actor")
        if command == "export":
            sub.add_argument("--include-sources", action="store_true")
        if command == "impact":
            sub.add_argument("--source",required=True)
            sub.add_argument("--replaces",required=True)
    return root


def main(argv=None):
    args, op = None, None
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args(argv)
        command = args.command
        if command in {"start", "session", "save", "choose", "report", "doctor", "export", "pause", "impact", "quality", "now", "track", "versions", "changes", "expand"}:
            from .experience import start, session, save, choose, pause, impact, quality, now_view, track, expand, active_project
            from .presentation import publish_report, doctor, export_project
            if command == "start":
                data = start(args.project, args.title, args.question, args.material, args.allow_url, args.synthetic, args.demo,
                             args.mode, args.quantitative, args.routing_reason,
                             args.depth or ("quick" if args.mode == "diagnosis" and not args.demo else "full"))
            elif command == "session":
                data = session(args.project)
            elif command == "save":
                data = save(args.project, args.input)
            elif command == "choose":
                data = choose(args.project, args.option, args.quote, args.actor)
            elif command == "pause":
                data = pause(args.project)
            elif command == "impact":
                data = impact(args.project,args.source,args.replaces)
            elif command == "quality":
                data = quality(args.project)
            elif command == "now":
                data = now_view(args.project)
            elif command == "track":
                data = track(args.project, args.input)
            elif command == "expand":
                data = expand(args.project, args.mode, args.reason)
            elif command in {"versions", "changes"}:
                from .versions import list_versions, compare
                study = Store(active_project(args.project))
                resume(study)
                data = list_versions(study) if command == "versions" else compare(study, args.from_version, args.to_version)
            elif command == "report":
                data = publish_report(args.project)
            elif command == "doctor":
                data = doctor(args.project)
                if not data["ready"]:
                    return emit(False, data, {"code": "VALIDATION_ERROR", "message": "Проверка готовности обнаружила проблемы.", "retryable": False}, None)
            else:
                data = export_project(args.project, args.include_sources)
            return emit(True, data, None, None)
        readonly = command in {"status", "resume", "validate", "profile-table"}
        op = None if readonly else args.operation_id
        path = project_path(args.project)
        if command == "init":
            result = init_project(path, op)
        else:
            store = Store(path)
            store.state()
            params = {k: v for k, v in vars(args).items() if k not in {"command", "project", "operation_id"}}
            # Include contents, not just draft/input filenames, in the idempotency fingerprint.
            for key in ["input", "spec", "file"]:
                if params.get(key):
                    params[key + "_sha256"] = digest(store.read(params[key]))
            if command == "status":
                result = {"data": {"state": store.state(), "pending_operations": store.pending()}, "warnings": []}
            elif command == "resume":
                result = {"data": resume(store), "warnings": []}
            elif command == "validate":
                report = validation(store)
                if not report["valid"]:
                    return emit(False, None, {"code": "VALIDATION_ERROR", "message": "Project integrity check failed.",
                                             "retryable": False, "details": report}, None, report["warnings"])
                result = {"data": report, "warnings": report["warnings"]}
            elif command == "profile-table":
                result = {"data": profile(store, args.source, args.sheet, args.date_column), "warnings": []}
            elif command == "recover":
                result = store.recover(op, args.action)
            else:
                def action():
                    if command == "ingest-file":
                        return ingest_file(store, args.file, args.delimiter, args.encoding, args.provenance)
                    if command == "ingest-url":
                        return ingest_url(store, args.url)
                    if command == "aggregate":
                        return aggregate(store, args.source, store.json(args.spec))
                    if command == "record-claim":
                        return record_claim(store, store.json(args.input))
                    if command == "record-decision":
                        return record_decision(store, store.json(args.input))
                    if command == "retract-claim":
                        return retract_claim(store, args.claim, args.reason)
                    if command == "checkpoint":
                        return checkpoint(store, store.json(args.input), args.expected_revision, op)
                    if command == "configure":
                        policy = Policy.model_validate(store.json(args.input)).model_dump()
                        policy["allowed_urls"] = list(dict.fromkeys(canonical(u) for u in policy["allowed_urls"]))
                        return {"configured": True}, {"policy.json": encode(policy)}, []
                    if command == "write-artifact":
                        target = args.path
                        store.path(target)
                        if target not in {"brief.md", "research-plan.md"} and not (target.startswith("reports/") and target.endswith(".md")):
                            fail("ACCESS_DENIED", "Artifacts are brief.md, research-plan.md and reports/*.md only.")
                        payload = store.read(args.input)
                        try:
                            if not payload.decode("utf-8").strip():
                                fail("INVALID_INPUT", "Empty report.")
                        except UnicodeError:
                            fail("INVALID_INPUT", "Reports must be UTF-8.")
                        return {"path": target, "sha256": digest(payload)}, {target: payload}, []
                    fail("INVALID_INPUT", "Unknown command.")
                result = store.mutate(op, command, params, action)
        return emit(True, result["data"], None, op, result.get("warnings", []))
    except AgentError as exc:
        return emit(False, None, {"code": exc.code, "message": exc.message, "retryable": exc.retryable}, op)
    except ValidationError as exc:
        errors = [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors(include_input=False, include_url=False)]
        return emit(False, None, {"code": "VALIDATION_ERROR", "message": "Invalid record fields.", "details": errors, "retryable": False}, op)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return emit(False, None, {"code": "PARSE_ERROR", "message": f"Operation failed ({type(exc).__name__}); check input and permissions.", "retryable": False}, op)
    except Exception as exc:
        return emit(False, None, {"code": "PARSE_ERROR", "message": f"Tool failed ({type(exc).__name__}); no successful result was recorded.", "retryable": False}, op)


def emit(ok, data, error, operation_id, warnings=None):
    print(json.dumps({"schema_version": 1, "ok": ok, "operation_id": operation_id, "data": data,
                      "warnings": warnings or [], "error": error}, ensure_ascii=False, allow_nan=False))
    return 0 if ok else 1
