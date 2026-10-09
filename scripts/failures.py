"""Print failed cases from a benchmark report (development splits only)."""
from __future__ import annotations

import json
import sys

report = json.load(open(sys.argv[1]))
if report["meta"].get("split") == "test":
    sys.exit("refusing to print held-out test cases")
filt = sys.argv[2] if len(sys.argv) > 2 else None
for case in report["cases"]:
    if case["correct"] or (filt and filt not in case["errors"] and filt != case["category"]):
        continue
    print(f"== {case['key']} [{case['hardness']}/{case['category']}] {case['errors']}")
    print("Q:", case["question"])
    print("G:", case["reference_sql"])
    print("P:", case["predicted_sql"], case["params"] or "", case["message"] or "")
    trace = case.get("trace") or {}
    if trace.get("explanation"):
        print("E:", trace["explanation"])
