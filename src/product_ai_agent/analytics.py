"""Small declarative aggregations: no expressions, eval, or implicit metrics."""
from decimal import Decimal, InvalidOperation, localcontext

from . import __version__
from .models import Calculation
from .storage import digest, encode, fail, now


def number(value):
    if isinstance(value, bool) or value is None:
        fail("INVALID_INPUT", f"Not a numeric value: {value!r}")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        fail("INVALID_INPUT", f"Not an unambiguous decimal number: {value!r}")
    if not result.is_finite():
        fail("INVALID_INPUT", "Non-finite number.")
    return result


def table_for(store, source_id, sheet=None):
    source = store.source(source_id)
    tables = store.json(source["extracted_path"])["tables"]
    matches = [t for t in tables if t["sheet"] == sheet]
    if len(matches) != 1:
        fail("INVALID_INPUT", "Select an existing XLSX sheet explicitly; CSV uses sheet=null.")
    return source, matches[0]


def profile(store, source_id, sheet=None, date_column=None):
    _, table = table_for(store, source_id, sheet)
    columns = {}
    for column in table["columns"]:
        values = [row[column] for row in table["rows"] if row[column] not in (None, "")]
        kinds = set()
        for value in values:
            try:
                number(value)
                kinds.add("numeric" if not isinstance(value, str) else "numeric_text")
            except Exception:
                kinds.add("text" if isinstance(value, str) else type(value).__name__)
        columns[column] = {"missing": len(table["rows"]) - len(values), "observed_types": sorted(kinds)}
    dates = None
    if date_column:
        from datetime import datetime
        if date_column not in columns:
            fail("INVALID_INPUT", "Unknown date column.")
        try:
            values = [datetime.fromisoformat(str(r[date_column])) for r in table["rows"] if r[date_column]]
            dates = {"column": date_column, "min": min(values).isoformat(), "max": max(values).isoformat()} if values else None
        except (ValueError, TypeError):
            fail("INVALID_INPUT", "Dates must be explicit comparable ISO dates/times.")
    return {"source_id": source_id, "sheet": sheet, "records": len(table["rows"]), "columns": columns,
            "date_range": dates, "warnings": ["Types describe observed values, not business meaning."]}


def matches(row, filters):
    for item in filters:
        value, target = row[item.column], item.value
        if value in (None, ""):
            return False
        if item.operator == "eq":
            same = value == target if isinstance(target, (str, bool)) else number(value) == number(target)
        elif item.operator == "in":
            if not isinstance(target, list):
                fail("INVALID_INPUT", "An in filter requires a list.")
            same = any(value == t if isinstance(t, (str, bool)) else number(value) == number(t) for t in target)
        elif item.operator == "gte":
            same = number(value) >= number(target)
        else:
            same = number(value) <= number(target)
        if not same:
            return False
    return True


def aggregate(store, source_id, raw_spec, tool_version=None):
    # User-experience releases do not reinterpret saved calculation receipts.
    tool_version = tool_version or __version__
    if tool_version not in {"0.1.0", "0.2.0", "0.3.0", "0.4.0"}:
        fail("VALIDATION_ERROR", "Unsupported calculation version; do not silently reinterpret old results.")
    spec = Calculation.model_validate(raw_spec)
    source, table = table_for(store, source_id, spec.sheet)
    if spec.operation == "ratio_of_sums":
        if not spec.numerator_column or not spec.denominator_column or spec.value_column:
            fail("INVALID_INPUT", "ratio_of_sums requires numerator_column and denominator_column; value_column=null.")
        value_columns = [spec.numerator_column, spec.denominator_column]
    elif spec.operation == "count_rows":
        if spec.value_column or spec.numerator_column or spec.denominator_column:
            fail("INVALID_INPUT", "count_rows does not accept value columns.")
        value_columns = []
    else:
        if not spec.value_column or spec.numerator_column or spec.denominator_column:
            fail("INVALID_INPUT", "sum/mean require value_column only.")
        value_columns = [spec.value_column]
    requested = set(spec.group_by + value_columns + [f.column for f in spec.filters])
    if spec.eligibility_column:
        requested.add(spec.eligibility_column)
    if not requested.issubset(table["columns"]):
        fail("INVALID_INPUT", f"Unknown columns: {sorted(requested - set(table['columns']))}")
    groups, excluded, filtered = {}, [], []
    for row_number, row in zip(table["row_numbers"], table["rows"]):
        if not matches(row, spec.filters):
            filtered.append(row_number)
            continue
        if spec.eligibility_column:
            eligibility = row[spec.eligibility_column]
            if eligibility not in {True, False, "true", "false", "1", "0", 1, 0}:
                fail("INVALID_INPUT", "Eligibility must be an explicit boolean/0/1.")
            if eligibility in {False, "false", "0", 0}:
                excluded.append({"record": row_number, "reason": "incomplete_observation_window"})
                continue
        missing = [c for c in spec.group_by + value_columns if row[c] in (None, "")]
        if missing:
            if spec.missing_policy == "error":
                fail("INVALID_INPUT", f"Missing {missing} at record {row_number}.")
            excluded.append({"record": row_number, "reason": "missing_values", "columns": missing})
            continue
        values = [number(row[c]) for c in value_columns]
        if spec.operation == "ratio_of_sums":
            if values[1] < 0 or (spec.bounded_ratio and (values[0] < 0 or values[0] > values[1])):
                fail("INVALID_INPUT", f"Invalid numerator/denominator at record {row_number}.")
        key = tuple(row[c] for c in spec.group_by)
        group = groups.setdefault(key, {"records": [], "values": [[] for _ in value_columns]})
        group["records"].append(row_number)
        for bucket, value in zip(group["values"], values):
            bucket.append(value)
    if not groups:
        fail("INVALID_INPUT", "No eligible records remain after filters and exclusions.")
    results = []
    with localcontext() as ctx:
        ctx.prec = 28
        for key, group in groups.items():
            sums = [sum(values, Decimal(0)) for values in group["values"]]
            if spec.operation == "count_rows":
                result = Decimal(len(group["records"]))
            elif spec.operation == "sum":
                result = sums[0]
            elif spec.operation == "mean":
                result = sums[0] / len(group["records"])
            else:
                if sums[1] == 0:
                    fail("INVALID_INPUT", f"Zero denominator for group {key}.")
                result = sums[0] / sums[1]
            results.append({"group": dict(zip(spec.group_by, key)), "value": str(result),
                            "numerator": str(sums[0]) if spec.operation == "ratio_of_sums" else None,
                            "denominator": str(sums[1]) if spec.operation == "ratio_of_sums" else None,
                            "used_records": group["records"]})
    saved_spec = spec.model_dump()
    calc_id = "calc-" + digest(encode({"source_id": source_id, "sha256": source["sha256"], "spec": saved_spec, "version": tool_version}))[:24]
    base = f"calculations/{calc_id}"
    result = {"schema_version": 1, "id": calc_id, "source_id": source_id, "source_sha256": source["sha256"],
              "spec_path": f"{base}/spec.json", "tool_version": tool_version, "created_at": now(),
              "groups": results, "excluded_records": excluded, "filtered_records": filtered,
              "warnings": ["Excluded records must be reviewed."] if excluded else []}
    if store.path(f"{base}/result.json").exists():
        existing = store.json(f"{base}/result.json")
        stable_keys = ["id", "source_id", "source_sha256", "spec_path", "tool_version", "groups", "excluded_records", "filtered_records", "warnings"]
        if any(existing.get(key) != result[key] for key in stable_keys) or store.json(f"{base}/spec.json") != saved_spec:
            fail("VALIDATION_ERROR", "Existing calculation does not match recomputation.")
        return {"calculation_id": calc_id, "path": f"{base}/result.json", "groups": results}, {}, result["warnings"]
    return {"calculation_id": calc_id, "path": f"{base}/result.json", "groups": results}, {
        f"{base}/spec.json": encode(saved_spec), f"{base}/result.json": encode(result)}, result["warnings"]
