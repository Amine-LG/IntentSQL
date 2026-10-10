"""Fit a confidence calibration table for one provider from its Spider *train* reports.

    python scripts/fit_calibration.py liquid ../IntentSQL-artifacts/v1-runs/liquid/train-*.json

Prints a table (score lower bound, observed accuracy) by isotonic regression of
the score (verification - 0.2 x alternatives) on execution accuracy, to be added
to CALIBRATIONS in intentsql/pipeline.py for that provider.  Refuses dev/test
reports and reports from another provider.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))

from intentsql.pipeline import ALTERNATIVE_PENALTY  # noqa: E402


def main(provider: str, paths: list[str]) -> None:
    points = []
    for path in paths:
        report = json.load(open(path))
        meta = report.get("meta", {})
        if meta.get("split") != "train":
            sys.exit(f"{path}: calibration is fitted on train reports only")
        if meta.get("provider") != provider:
            sys.exit(f"{path}: report is from provider {meta.get('provider')!r}, not {provider!r}")
        for case in report["cases"]:
            trace = case.get("trace") or {}
            verification = trace.get("verification")
            if case["status"] not in ("answered", "ambiguous") or verification is None:
                continue
            score = max(0.0, verification - ALTERNATIVE_PENALTY * len(trace.get("alternatives") or []))
            points.append((score, 1 if case["correct"] else 0))
    if len(points) < 300:
        sys.exit(f"only {len(points)} scored predictions; fit on at least 300")
    points.sort()
    blocks: list[list[float]] = []                  # [low, total correct, count]
    for score, correct in points:
        blocks.append([score, correct, 1])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            low, total, count = blocks.pop()
            blocks[-1][1] += total
            blocks[-1][2] += count
    table = []
    for low, total, count in blocks:
        if count >= 15 or not table:               # merge tiny blocks into their neighbour
            table.append([round(low, 2), total, count])
        else:
            table[-1][1] += total
            table[-1][2] += count
    print(f"# {provider}: {len(points)} train predictions")
    print("(" + ", ".join(f"({low}, {total / count:.2f})" for low, total, count in table) + ")")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
