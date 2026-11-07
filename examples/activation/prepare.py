"""Fixture setup only. Does not implement an agent or generate its answers."""
import argparse
import shutil
from pathlib import Path

from product_ai_agent.storage import Store, atomic, encode, init_project, project_path

parser = argparse.ArgumentParser()
parser.add_argument("--project", required=True)
args = parser.parse_args()
root = project_path(args.project)
init_project(root, "init-activation")
store = Store(root)
for name in ["product.md", "metrics.csv", "notes.md", "request.md"]:
    destination = store.path("inputs/" + name)
    payload = Path(__file__).with_name(name).read_bytes()
    if destination.exists() and destination.read_bytes() != payload:
        parser.error(f"Existing input differs; refusing overwrite: {name}")
    if not destination.exists():
        atomic(destination, payload)
spec = {"metric_name": "7-day activation", "definition": "Activated owners / owners with a complete 7x24h window; UTC; disjoint aggregates.",
        "sheet": None, "operation": "ratio_of_sums", "value_column": None,
        "numerator_column": "activated_7d", "denominator_column": "eligible_users",
        "group_by": ["period"], "filters": [], "missing_policy": "error", "units": "proportion",
        "period": {"from": "2026-09", "to": "2026-10", "as_of": "2026-11-07", "timezone": "UTC"},
        "eligibility_column": "complete_window", "bounded_ratio": True}
for filename, groups in [("overall.json", ["period"]), ("by-channel.json", ["period", "channel"])]:
    target = store.path("drafts/" + filename)
    payload = encode({**spec, "group_by": groups})
    if not target.exists():
        atomic(target, payload)
print("Prepared synthetic inputs and calculation specifications. Start the research through instructions/entry.md.")
