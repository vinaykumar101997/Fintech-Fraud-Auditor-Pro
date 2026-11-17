"""Discordant cases between two runs of scripts/eval_llm_tier.py on the same cases.

Reads two --cases-out files, pairs the rows by case id and counts, separately
for expected SUSPICIOUS and expected CLEAR cases:

    b  cases run A got right and run B got wrong
    c  cases run B got right and run A got wrong

An ERROR is never right. Cases present in only one file are listed, not paired.

    python scripts/compare_llm_runs.py data/agent_run1.jsonl data/fixed_run1.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_llm_tier import read_rows  # noqa: E402

CLASSES = ("SUSPICIOUS", "CLEAR")


def _right(row: Dict[str, Any]) -> bool:
    return row["returned"] == row["expected"]


def discordant(rows_a: List[Dict[str, Any]], rows_b: List[Dict[str, Any]]) -> Dict[str, Any]:
    a = {r["id"]: r for r in rows_a}
    b = {r["id"]: r for r in rows_b}
    paired = [cid for cid in a if cid in b]
    out: Dict[str, Any] = {"paired": len(paired),
                           "only_a": sorted(set(a) - set(b)), "only_b": sorted(set(b) - set(a))}
    for cls in CLASSES:
        ids = [cid for cid in paired if a[cid]["expected"] == cls]
        out[cls] = {
            "n": len(ids),
            "a_right": sum(_right(a[cid]) for cid in ids),
            "b_right": sum(_right(b[cid]) for cid in ids),
            "b": sorted(cid for cid in ids if _right(a[cid]) and not _right(b[cid])),
            "c": sorted(cid for cid in ids if _right(b[cid]) and not _right(a[cid])),
        }
    return out


def report_lines(result: Dict[str, Any], name_a: str, name_b: str) -> List[str]:
    lines = [f"A = {name_a}", f"B = {name_b}", f"Paired cases: {result['paired']}", "",
             "| Expected | Cases | A right | B right | b (A right, B wrong) | c (B right, A wrong) |",
             "|---|---|---|---|---|---|"]
    for cls in CLASSES:
        r = result[cls]
        lines.append(f"| {cls} | {r['n']} | {r['a_right']} | {r['b_right']} | "
                     f"{len(r['b'])} {', '.join(r['b'])} | {len(r['c'])} {', '.join(r['c'])} |")
    for key, name in (("only_a", name_a), ("only_b", name_b)):
        if result[key]:
            lines.append(f"Only in {name}: {', '.join(result[key])}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_a", type=Path)
    parser.add_argument("run_b", type=Path)
    args = parser.parse_args(argv)
    result = discordant(read_rows(args.run_a), read_rows(args.run_b))
    print("\n".join(report_lines(result, args.run_a.name, args.run_b.name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
