import io
import json
from pathlib import Path

import pytest

from product_ai_agent.analytics import aggregate, profile
from product_ai_agent.cli import main
from product_ai_agent.ingestion import ingest_file, parse_file
from product_ai_agent.models import Policy
from product_ai_agent.storage import AgentError, Store, atomic, encode, init_project, project_path
from product_ai_agent.web import PinnedHTTP, canonical, fetch_html, resolve_public
from product_ai_agent.workflow import checkpoint, record_claim, record_decision, resume, validation


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "проект с пробелом"
    # Project ID is intentionally portable; paths in parent directories may be Unicode/spaces.
    root = root / "demo"
    init_project(root, "init-test")
    return Store(root)


def put(store, path, value):
    atomic(store.path(path), value if isinstance(value, bytes) else encode(value))


def import_csv(store, text="segment,numerator,denominator,eligible\na,2,5,true\na,1,5,true\nb,9,10,false\n"):
    put(store, "inputs/data.csv", text.encode())
    result = store.mutate("import-csv", "ingest-file", {"file": "inputs/data.csv"}, lambda: ingest_file(store, "inputs/data.csv", ",", provenance="synthetic"))
    return result["data"]["source_id"]


def ratio_spec(**changes):
    return {"metric_name": "conversion", "definition": "Numerator over eligible denominator; complete windows only.",
            "operation": "ratio_of_sums", "numerator_column": "numerator", "denominator_column": "denominator",
            "group_by": ["segment"], "eligibility_column": "eligible", "bounded_ratio": True,
            "units": "proportion", **changes}


def test_init_no_overwrite_and_empty_state(store):
    assert store.state()["revision"] == 0
    assert store.registry("sources.jsonl") == []
    init_project(store.root, "init-test")
    with pytest.raises(AgentError):
        init_project(store.root, "another-id")
    assert validation(store)["valid"]


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "inputs/../../outside", "inputs\\a"])
def test_confined_paths(store, path):
    with pytest.raises(AgentError, match="Path|paths|relative"):
        store.path(path)


def test_workspace_boundary(tmp_path):
    with pytest.raises(AgentError):
        project_path("../escape", tmp_path)


def test_import_idempotency_and_tamper_detection(store):
    source_id = import_csv(store)
    source = store.source(source_id)
    result = store.mutate("another-import", "ingest-file", {}, lambda: ingest_file(store, "inputs/data.csv", ",", provenance="synthetic"))
    assert result["data"]["source_id"] == source_id
    assert len(store.registry("sources.jsonl")) == 1
    put(store, source["snapshot_path"], b"tampered")
    assert not validation(store)["valid"]


def test_csv_multiline_records_and_errors():
    parsed, _ = parse_file(b'name,value\n"one\ntwo",3\nthree,4\n', ".csv", ",")
    assert len(parsed["tables"][0]["rows"]) == 2
    assert parsed["blocks"][0]["block_id"] == "record-000001"
    for payload in [b"a,a\n1,2\n", b"a,b\n1\n", b"a,b\n\n", b""]:
        with pytest.raises(AgentError):
            parse_file(payload, ".csv", ",")
    with pytest.raises(AgentError):
        parse_file(b"a,b\n1,2", ".pdf")


def test_docx_table_and_xlsx_multiple_sheets():
    from docx import Document
    from openpyxl import Workbook
    doc = Document()
    doc.add_paragraph("A real paragraph")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "A table cell"
    stream = io.BytesIO()
    doc.save(stream)
    parsed, _ = parse_file(stream.getvalue(), ".docx")
    assert parsed["blocks"][0]["text"] == "A real paragraph"
    assert parsed["blocks"][1]["rows"] == [["A table cell"]]
    book = Workbook()
    for sheet in [book.active, book.create_sheet("Other")]:
        sheet.append(["value"])
        sheet.append([42])
    stream = io.BytesIO()
    book.save(stream)
    parsed, _ = parse_file(stream.getvalue(), ".xlsx")
    assert len(parsed["tables"]) == 2
    book.active["A2"] = "=1+1"
    stream = io.BytesIO()
    book.save(stream)
    with pytest.raises(AgentError, match="cached"):
        parse_file(stream.getvalue(), ".xlsx")


def test_ratio_control_and_incomplete_window(store):
    source_id = import_csv(store)
    data, writes, _ = aggregate(store, source_id, ratio_spec())
    assert data["groups"][0]["value"] == "0.3"
    result = next(json.loads(v) for k, v in writes.items() if k.endswith("result.json"))
    assert result["excluded_records"] == [{"record": 3, "reason": "incomplete_observation_window"}]
    assert data["groups"][0]["denominator"] == "10"


@pytest.mark.parametrize("row", ["a,1,0,true", "a,6,5,true", "a,,5,true", "a,NaN,5,true", "a,1,5,maybe"])
def test_bad_metrics_rejected(store, row):
    source_id = import_csv(store, "segment,numerator,denominator,eligible\n" + row + "\n")
    with pytest.raises(AgentError):
        aggregate(store, source_id, ratio_spec())


def test_missing_exclusion_and_generic_methods(store):
    source_id = import_csv(store, "segment,numerator,denominator,eligible\na,2,5,true\na,,5,true\n")
    data, _, _ = aggregate(store, source_id, ratio_spec(missing_policy="exclude"))
    assert data["groups"][0]["value"] == "0.4"
    assert profile(store, source_id)["columns"]["numerator"]["missing"] == 1
    for operation, expected in [("sum", "2"), ("mean", "2"), ("count_rows", "2")]:
        spec = {"metric_name": operation, "definition": "Test definition", "operation": operation,
                "units": "items", "missing_policy": "exclude", "value_column": None if operation == "count_rows" else "numerator"}
        data, _, _ = aggregate(store, source_id, spec)
        assert data["groups"][0]["value"] == expected


def test_operation_replay_conflict_and_failure(store):
    calls = []
    def action():
        calls.append(1)
        return {"value": 1}, {"reports/test.md": b"answer"}, []
    first = store.mutate("once", "test", {"x": 1}, action)
    assert store.mutate("once", "test", {"x": 1}, action) == first
    assert len(calls) == 1
    with pytest.raises(AgentError, match="different inputs"):
        store.mutate("once", "test", {"x": 2}, action)
    def failing():
        raise AgentError("INVALID_INPUT", "test failure")
    with pytest.raises(AgentError):
        store.mutate("failed", "test", {}, failing)
    assert not store.pending()


def test_prepared_interruption_rolls_forward(store, monkeypatch):
    original = store.apply_manifest
    def interrupt(manifest):
        first = manifest["writes"][0]
        atomic(store.path(first["target"]), store.read(first["staged"]))
        raise OSError("simulated interruption between files")
    monkeypatch.setattr(store, "apply_manifest", interrupt)
    with pytest.raises(AgentError, match="interrupted"):
        store.mutate("interrupt", "test", {}, lambda: ({"saved": True}, {"reports/a.md": b"A", "reports/b.md": b"B"}, []))
    assert store.pending()
    assert not validation(store)["valid"]
    with pytest.raises(AgentError):
        store.recover("interrupt", "abort")
    monkeypatch.setattr(store, "apply_manifest", original)
    assert store.recover("interrupt", "finish")["data"]["saved"]
    assert store.read("reports/b.md") == b"B"
    assert not store.pending()


def test_unprepared_interruption_can_abort(store):
    store.event("unprepared", "started", input_hash="abc", command="test")
    assert store.recover("unprepared", "abort")["data"]["aborted"]
    assert not store.pending()


def test_checkpoint_revision_and_new_reader(store):
    put(store, "brief.md", b"A genuine brief with a decision question")
    state = store.state()
    state.update(stage="research", completed_stages=["brief"], artifacts={"brief": "brief.md"}, next_action="Import materials")
    store.mutate("checkpoint", "checkpoint", {}, lambda: checkpoint(store, state, 0, "checkpoint"))
    fresh_reader = Store(store.root)
    assert resume(fresh_reader)["state"]["next_action"] == "Import materials"
    with pytest.raises(AgentError, match="revision"):
        checkpoint(store, state, 0, "stale")
    assert fresh_reader.json("checkpoints/rev-000001/manifest.json")["hashes"]


def test_no_fake_completion_or_decision(store):
    state = store.state()
    state.update(stage="complete", status="completed")
    assert not validation(store, state)["valid"]
    with pytest.raises(Exception):
        record_decision(store, {"id": "fake"})


def test_claim_locator_and_synthetic_source(store):
    source_id = import_csv(store)
    claim = {"id": "clm-1", "text": "A test observation", "type": "observation", "evidence": [{"source_id": source_id, "locator": "record:1"}],
             "calculation_ids": [], "basis_claim_ids": [], "limitations": ["Synthetic"], "status": "active", "supersedes": None,
             "created_at": "2026-11-07T12:00:00Z"}
    store.mutate("claim", "record-claim", {}, lambda: record_claim(store, claim))
    assert validation(store)["valid"]
    claim.update(id="clm-2", evidence=[{"source_id": source_id, "locator": "record:999"}])
    with pytest.raises(AgentError):
        record_claim(store, claim)


@pytest.mark.parametrize("url", ["http://u:p@example.org/", "file:///etc/passwd", "https://example.org:8443/", "https://example.org/#x", "http://example.org\\@localhost/"])
def test_bad_urls(url):
    with pytest.raises(AgentError):
        canonical(url)


def test_web_allowlist_redirect_and_nonpublic_dns():
    policy = Policy(allowed_urls=["https://public.example/"]).model_dump()
    calls = []
    def resolver(host, port):
        return ["93.184.216.34"]
    def transport(*args):
        calls.append(args)
        return 302, {"location": "http://127.0.0.1/"}, b""
    with pytest.raises(AgentError):
        fetch_html("https://public.example/", policy, resolver, transport)
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(AgentError):
        fetch_html("https://public.example/", policy, lambda *a: ["127.0.0.1"], transport)
    assert not calls
    with pytest.raises(AgentError):
        fetch_html("https://public.example.attacker/", policy, resolver, transport)


def test_dns_rebinding_cannot_change_dialled_ip(monkeypatch):
    # Transport must connect to the already validated numeric IP, not resolve host again.
    seen = []
    class FakeSocket:
        def getpeername(self):
            return ("127.0.0.1", 80)
        def close(self):
            pass
    def connect(address, timeout):
        seen.append(address)
        return FakeSocket()
    monkeypatch.setattr("product_ai_agent.web.socket.create_connection", connect)
    connection = PinnedHTTP("public.example", 80, "93.184.216.34", False, 1)
    with pytest.raises(AgentError):
        connection.connect()
    assert seen == [("93.184.216.34", 80)]


def test_cli_json_error_and_no_secret_echo(capsys):
    assert main(["ingest-file"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"]
    assert result["error"]["code"] == "INVALID_INPUT"


def test_changed_calculation_result_is_detected(store):
    source_id = import_csv(store)
    data, writes, _ = aggregate(store, source_id, ratio_spec())
    for path, payload in writes.items():
        put(store, path, payload)
    path = data["path"]
    result = store.json(path)
    result["groups"][0]["value"] = "0.9"
    put(store, path, result)
    assert not validation(store)["valid"]


def test_absolute_http_read_deadline(monkeypatch):
    from product_ai_agent.web import DeadlineReader
    class FakeSocket:
        def settimeout(self, value):
            pass
        def recv_into(self, buffer):
            return 1
    reader = DeadlineReader(FakeSocket(), 20)
    monkeypatch.setattr("product_ai_agent.web.time.monotonic", lambda: 21)
    with pytest.raises(TimeoutError):
        reader.readinto(bytearray(10))


def test_symlink_escape_if_os_allows(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (store.root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow creating symlinks for this account")
    with pytest.raises(AgentError):
        store.path("linked/secret.txt")


def test_replayed_import_checks_immutable_outputs(store):
    source_id = import_csv(store)
    put(store, store.source(source_id)["snapshot_path"], b"changed")
    with pytest.raises(AgentError, match="Immutable"):
        store.mutate("import-csv", "ingest-file", {"file": "inputs/data.csv"}, lambda: None)


def test_connection_close_keeps_response_reader_alive():
    import time
    from product_ai_agent.web import DeadlineSocket
    class FakeSocket:
        closed = False
        def settimeout(self, value):
            pass
        def recv_into(self, buffer):
            assert not self.closed
            buffer[0] = 65
            return 1
        def close(self):
            self.closed = True
    raw = FakeSocket()
    wrapped = DeadlineSocket(raw, time.monotonic() + 20)
    reader = wrapped.makefile("rb")
    wrapped.close()
    assert not raw.closed
    assert reader.read(1) == b"A"
    reader.close()
    assert raw.closed


def test_windows_junction_is_rejected(store, tmp_path):
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows junction check")
    outside = tmp_path / "junction-target"
    outside.mkdir()
    link = store.root / "junction"
    process = subprocess.run(["powershell", "-NoProfile", "-Command",
        "New-Item -ItemType Junction -Path $env:AGENT_TEST_LINK -Target $env:AGENT_TEST_TARGET | Out-Null"],
        env={**os.environ, "AGENT_TEST_LINK": str(link), "AGENT_TEST_TARGET": str(outside)}, capture_output=True)
    assert process.returncode == 0
    try:
        with pytest.raises(AgentError):
            store.path("junction/file.txt")
    finally:
        link.rmdir()  # Removes this junction only, not the target directory.


def test_web_redirects_limits_and_retry():
    policy = Policy(allowed_urls=["https://public.example/", "https://public.example/page"]).model_dump()
    resolver = lambda *a: ["93.184.216.34"]
    calls = []
    def transport(url, *args):
        calls.append(url)
        if url.endswith("/"):
            return 302, {"location": "/page"}, b""
        return 200, {"content-type": "text/html"}, b"<p>content</p>"
    body, final = fetch_html("https://public.example/", policy, resolver, transport)
    assert final.endswith("/page") and body == b"<p>content</p>" and len(calls) == 2
    policy["max_bytes"] = 2
    with pytest.raises(AgentError, match="byte limit"):
        fetch_html("https://public.example/", policy, resolver, transport)
    attempts = []
    def failing_transport(*args):
        attempts.append(1)
        raise AgentError("NETWORK_ERROR", "temporary test outage", True)
    with pytest.raises(AgentError):
        fetch_html("https://public.example/", policy, resolver, failing_transport)
    assert len(attempts) == policy["max_retries"] + 1


def test_real_loopback_is_blocked_before_transport():
    called = []
    with pytest.raises(AgentError, match="non-public"):
        fetch_html("http://127.0.0.1/", Policy(allowed_urls=["http://127.0.0.1/"]).model_dump(),
                   transport=lambda *a: called.append(1))
    assert not called
