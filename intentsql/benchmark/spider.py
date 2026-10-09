"""Spider 1.0 data access, official hardness levels and construct tags.

Splits are database-disjoint in Spider.  IntentSQL uses them as:

* ``train``  -- development: architecture iteration and failure inspection;
* ``dev``    -- validation: periodic checks, aggregate statistics only;
* ``test``   -- held-out: milestone evaluation, never inspected per case.

Nothing here reads a prediction; sampling depends only on the reference SQL.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

SPLIT_FILES = {
    "train": ("train_spider.json", "database", "tables.json"),
    "dev": ("dev.json", "database", "tables.json"),
    "test": ("test.json", "test_database", "test_tables.json"),
}
HARDNESS = ("easy", "medium", "hard", "extra")


def spider_root() -> Path:
    """Locate the extracted ``spider_data`` directory."""
    explicit = os.environ.get("INTENTSQL_SPIDER")
    candidates = [Path(explicit)] if explicit else []
    artifacts = os.environ.get("INTENTSQL_ARTIFACTS")
    if artifacts:
        candidates.append(Path(artifacts) / "spider" / "spider_data")
    here = Path(__file__).resolve().parents[2]
    candidates += [here.parent / "IntentSQL-artifacts" / "spider" / "spider_data",
                   here / "spider_data"]
    for candidate in candidates:
        if (candidate / "dev.json").is_file():
            return candidate
    raise FileNotFoundError(
        "Spider data not found. Set INTENTSQL_SPIDER to the extracted spider_data directory.")


@dataclass(frozen=True)
class Example:
    split: str
    index: int
    db_id: str
    question: str
    query: str
    hardness: str
    tags: tuple[str, ...]
    db_path: Path = field(compare=False)

    @property
    def key(self) -> str:
        return f"{self.split}:{self.index}"


# --- Official Spider hardness (evaluation.py, Yu et al. 2018) ----------------

_WHERE_LIKE = 9  # WHERE_OPS.index('like')


def _has_agg(unit: Any) -> bool:
    return unit[0] != 0


def _count_agg(units: Iterable[Any]) -> int:
    return len([unit for unit in units if _has_agg(unit)])


def _nested(sql: dict) -> list[dict]:
    nested = []
    for cond in sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]:
        if isinstance(cond[3], dict):
            nested.append(cond[3])
        if isinstance(cond[4], dict):
            nested.append(cond[4])
    for key in ("intersect", "except", "union"):
        if sql[key] is not None:
            nested.append(sql[key])
    return nested


def _component1(sql: dict) -> int:
    count = 0
    count += len(sql["where"]) > 0
    count += len(sql["groupBy"]) > 0
    count += len(sql["orderBy"]) > 0
    count += sql["limit"] is not None
    if sql["from"]["table_units"]:
        count += len(sql["from"]["table_units"]) - 1
    connectors = sql["from"]["conds"][1::2] + sql["where"][1::2] + sql["having"][1::2]
    count += len([c for c in connectors if c == "or"])
    conds = sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]
    count += len([c for c in conds if c[1] == _WHERE_LIKE])
    return count


def _others(sql: dict) -> int:
    count = 0
    agg = _count_agg(sql["select"][1])
    agg += _count_agg(sql["where"][::2])
    agg += _count_agg(sql["groupBy"])
    if sql["orderBy"]:
        agg += _count_agg([u[1] for u in sql["orderBy"][1] if u[1]]
                          + [u[2] for u in sql["orderBy"][1] if u[2]])
    agg += _count_agg(sql["having"])
    count += agg > 1
    count += len(sql["select"][1]) > 1
    count += len(sql["where"]) > 1
    count += len(sql["groupBy"]) > 1
    return count


def hardness(sql: dict) -> str:
    c1, c2, others = _component1(sql), len(_nested(sql)), _others(sql)
    if c1 <= 1 and others == 0 and c2 == 0:
        return "easy"
    if (others <= 2 and c1 <= 1 and c2 == 0) or (c1 <= 2 and others < 2 and c2 == 0):
        return "medium"
    if ((others > 2 and c1 <= 2 and c2 == 0) or (2 < c1 <= 3 and others <= 2 and c2 == 0)
            or (c1 <= 1 and others == 0 and c2 <= 1)):
        return "hard"
    return "extra"


# --- Construct tags (from reference SQL text only) ------------------------------

def construct_tags(query: str) -> tuple[str, ...]:
    q = " " + " ".join(query.upper().split()) + " "
    tags = []
    joins = q.count(" JOIN ")
    if joins >= 2:
        tags.append("multi_join")
    elif joins == 1:
        tags.append("join")
    if re.search(r"\(\s*SELECT", q):
        tags.append("nested")
    if any(op in q for op in (" INTERSECT ", " UNION ", " EXCEPT ")):
        tags.append("set_op")
    if " GROUP BY " in q:
        tags.append("having" if " HAVING " in q else "group")
    if " ORDER BY " in q:
        tags.append("order_limit" if " LIMIT " in q else "order")
    if re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", q):
        tags.append("aggregate")
    if " WHERE " in q:
        tags.append("filter")
    if not tags:
        tags.append("projection")
    return tuple(tags)


def primary_category(tags: tuple[str, ...]) -> str:
    """One stratum per example, most structurally demanding construct first."""
    for tag in ("set_op", "nested", "having", "multi_join", "group", "join", "order_limit",
                "order", "aggregate", "filter", "projection"):
        if tag in tags:
            return tag
    return "projection"


# --- Loading and sampling -------------------------------------------------------

@lru_cache(maxsize=4)
def load_split(split: str, root: str | None = None) -> tuple[Example, ...]:
    base = Path(root) if root else spider_root()
    data_file, db_dir, _ = SPLIT_FILES[split]
    raw = json.loads((base / data_file).read_text(encoding="utf-8"))
    examples = []
    for index, item in enumerate(raw):
        db_path = base / db_dir / item["db_id"] / f"{item['db_id']}.sqlite"
        examples.append(Example(
            split=split, index=index, db_id=item["db_id"], question=item["question"],
            query=item["query"], hardness=hardness(item["sql"]),
            tags=construct_tags(item["query"]), db_path=db_path))
    return tuple(examples)


def stratified_sample(examples: Iterable[Example], size: int, seed: int, *,
                      by: str = "hardness") -> list[Example]:
    """Draw ``size`` examples proportionally across strata, with a seeded RNG.

    Every stratum present receives at least one example when ``size`` allows, so
    small random batches still cover hard and extra-hard requests.
    """
    rng = random.Random(seed)
    strata: dict[str, list[Example]] = defaultdict(list)
    for example in examples:
        key = example.hardness if by == "hardness" else primary_category(example.tags)
        strata[key].append(example)
    total = sum(len(group) for group in strata.values())
    if size >= total:
        chosen = [e for group in strata.values() for e in group]
        rng.shuffle(chosen)
        return chosen
    quotas = {key: max(1, round(size * len(group) / total)) for key, group in strata.items()}
    while sum(quotas.values()) > size:
        largest = max(quotas, key=lambda k: quotas[k])
        quotas[largest] -= 1
    while sum(quotas.values()) < size:
        key = max(strata, key=lambda k: len(strata[k]) / max(quotas[k], 1))
        quotas[key] += 1
    chosen = []
    for key in sorted(strata):
        chosen.extend(rng.sample(strata[key], min(quotas[key], len(strata[key]))))
    rng.shuffle(chosen)
    return chosen
