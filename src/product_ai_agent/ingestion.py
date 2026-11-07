"""Bounded document/table extraction. Source text is never executable."""
import csv
import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

from . import __version__
from .storage import digest, encode, fail, now, registry_bytes

MAX_CELLS = 200_000


def check_archive(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 10000 or sum(x.file_size for x in archive.infolist()) > 40 * 1024 * 1024:
                fail("LIMIT_EXCEEDED", "Uncompressed office document exceeds the extraction limit.")
    except zipfile.BadZipFile:
        fail("PARSE_ERROR", "Invalid DOCX/XLSX archive.")


def scalar(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        fail("PARSE_ERROR", "Non-finite spreadsheet number.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def headers(values):
    result = [str(v).strip() if v is not None else "" for v in values]
    if not result or any(not v for v in result) or len(set(result)) != len(result):
        fail("PARSE_ERROR", "Table headers must be nonempty and unique.")
    return result


def parse_file(data, suffix, delimiter=None, encoding="utf-8-sig"):
    blocks, tables, warnings = [], [], []
    if not data:
        fail("PARSE_ERROR", "Empty input file.")
    if suffix in {".md", ".txt", ".csv"}:
        if encoding.lower() not in {"utf-8", "utf-8-sig"}:
            fail("UNSUPPORTED_FORMAT", "V0 accepts UTF-8 text only; convert other encodings explicitly.")
        try:
            text = data.decode(encoding)
        except UnicodeError:
            fail("PARSE_ERROR", "Input must be UTF-8.")
        if "\x00" in text or not text.strip():
            fail("PARSE_ERROR", "Empty or binary text input.")
        if suffix != ".csv":
            blocks = [{"block_id": f"line-{n:06}", "text": line} for n, line in enumerate(text.splitlines(), 1)]
        else:
            if delimiter is None:
                try:
                    delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
                except csv.Error:
                    fail("INVALID_INPUT", "Ambiguous CSV delimiter; pass --delimiter explicitly.")
            if delimiter not in {",", ";", "\t", "|"}:
                fail("INVALID_INPUT", "CSV delimiter must be comma, semicolon, tab or pipe.")
            try:
                reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
                columns = headers(next(reader))
                rows = []
                for number, row in enumerate(reader, 1):
                    if not row:
                        fail("PARSE_ERROR", f"Blank CSV record {number}; clean or explicitly represent missing values.")
                    if len(row) != len(columns):
                        fail("PARSE_ERROR", f"CSV record {number} has the wrong number of fields.")
                    if (number + 1) * len(columns) > MAX_CELLS:
                        fail("LIMIT_EXCEEDED", "Table cell limit exceeded.")
                    record = {key: value if value != "" else None for key, value in zip(columns, row)}
                    rows.append(record)
                    blocks.append({"block_id": f"record-{number:06}", "record": number, "values": record})
            except (csv.Error, StopIteration) as exc:
                fail("PARSE_ERROR", f"Invalid CSV: {exc}")
            if not rows:
                fail("PARSE_ERROR", "CSV contains headers but no records.")
            tables = [{"table_id": "csv", "sheet": None, "columns": columns, "rows": rows,
                       "row_numbers": list(range(1, len(rows) + 1)), "delimiter": delimiter}]
    elif suffix == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        check_archive(data)
        try:
            doc = Document(io.BytesIO(data))
            for index, item in enumerate(doc.iter_inner_content(), 1):
                if isinstance(item, Paragraph):
                    blocks.append({"block_id": f"p-{index:06}", "text": item.text})
                elif isinstance(item, Table):
                    rows = [[cell.text for cell in row.cells] for row in item.rows]
                    blocks.append({"block_id": f"table-{index:06}", "rows": rows})
                if len(blocks) > 20000:
                    fail("LIMIT_EXCEEDED", "Document block limit exceeded.")
        except (ValueError, KeyError, OSError) as exc:
            fail("PARSE_ERROR", f"Cannot parse DOCX: {type(exc).__name__}")
        warnings.append("DOCX: body text and top-level tables only; images, headers, footnotes and layout are not analysed.")
    elif suffix == ".xlsx":
        from openpyxl import load_workbook
        check_archive(data)
        formula_book = value_book = None
        try:
            formula_book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            value_book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
            cells = 0
            for sheet_name in formula_book.sheetnames:
                formula_sheet, value_sheet = formula_book[sheet_name], value_book[sheet_name]
                values = []
                for row_number, (frow, vrow) in enumerate(zip(formula_sheet.iter_rows(), value_sheet.iter_rows()), 1):
                    cells += len(frow)
                    if cells > MAX_CELLS:
                        fail("LIMIT_EXCEEDED", "Spreadsheet cell limit exceeded.")
                    record = []
                    for fcell, vcell in zip(frow, vrow):
                        if fcell.data_type == "f" and vcell.value is None:
                            fail("PARSE_ERROR", f"No cached formula value: {sheet_name}!{fcell.coordinate}")
                        if vcell.data_type == "e":
                            fail("PARSE_ERROR", f"Excel error: {sheet_name}!{vcell.coordinate}")
                        record.append(scalar(vcell.value))
                    values.append(record)
                while values and all(v is None for v in values[-1]):
                    values.pop()
                if not values:
                    warnings.append(f"Empty sheet skipped: {sheet_name}")
                    continue
                columns = headers(values[0])
                rows = [dict(zip(columns, row)) for row in values[1:]]
                tables.append({"table_id": f"sheet-{len(tables)+1}", "sheet": sheet_name,
                               "columns": columns, "rows": rows, "row_numbers": list(range(2, len(values)+1))})
                for row_number, row in enumerate(values, 1):
                    blocks.append({"block_id": f"sheet-{len(tables)}-row-{row_number}",
                                   "sheet": sheet_name, "row": row_number, "values": row})
        except (ValueError, KeyError, OSError) as exc:
            fail("PARSE_ERROR", f"Cannot parse XLSX: {type(exc).__name__}")
        finally:
            if formula_book is not None:
                formula_book.close()
            if value_book is not None:
                value_book.close()
        if not tables:
            fail("PARSE_ERROR", "Workbook contains no nonempty tables.")
        warnings.append("XLSX formulas are not calculated; cached values may be stale. First row is the header.")
    else:
        fail("UNSUPPORTED_FORMAT", f"Unsupported file format: {suffix or '(none)'}")
    if not blocks or not any(str(b.get("text", b.get("values", b.get("rows", "")))).strip() for b in blocks):
        fail("PARSE_ERROR", "No extractable content.")
    return {"schema_version": 1, "format": suffix[1:], "blocks": blocks, "tables": tables}, warnings


def source_output(store, data, extracted, suffix, title, origin, provenance, warnings, final_url=None):
    extracted_bytes = encode(extracted)
    # Provenance participates in identity: a synthetic source cannot silently become a real one.
    fingerprint = digest(encode({"raw": digest(data), "extracted": digest(extracted_bytes),
                                 "parser": __version__, "provenance": provenance}))
    source_id = f"src-{fingerprint[:24]}"
    rows = store.registry("sources.jsonl")
    existing = next((row for row in rows if row["id"] == source_id), None)
    if existing:
        store.source(source_id)
        return {"source_id": source_id, "path": existing["extracted_path"], "deduplicated": True}, {}, warnings
    base = f"sources/{source_id}"
    record = {"schema_version": 1, "id": source_id, "kind": "web" if final_url else ("table" if extracted["tables"] else "document"),
              "title": title, "origin": origin, "final_url": final_url, "retrieved_at": now(), "published_at": None,
              "snapshot_path": f"{base}/original{suffix}", "sha256": digest(data),
              "extracted_path": f"{base}/extracted.json", "extracted_sha256": digest(extracted_bytes),
              "parser_version": __version__, "provenance": provenance, "synthetic": provenance == "synthetic", "warnings": warnings}
    rows.append(record)
    writes = {record["snapshot_path"]: data, record["extracted_path"]: extracted_bytes,
              "sources.jsonl": registry_bytes(rows)}
    return {"source_id": source_id, "path": record["extracted_path"], "deduplicated": False}, writes, warnings


def ingest_file(store, filename, delimiter=None, encoding="utf-8-sig", provenance="user_provided"):
    path = store.path(filename)
    if not path.is_relative_to(store.path("inputs")):
        fail("ACCESS_DENIED", "Copy the input explicitly into this project's inputs/ directory first.")
    data = store.read(filename)
    if len(data) > store.policy()["max_bytes"]:
        fail("LIMIT_EXCEEDED", "Input exceeds the byte limit.")
    extracted, warnings = parse_file(data, path.suffix.lower(), delimiter, encoding)
    return source_output(store, data, extracted, path.suffix.lower(), path.name,
                         {"type": "file", "value": filename}, provenance, warnings)
