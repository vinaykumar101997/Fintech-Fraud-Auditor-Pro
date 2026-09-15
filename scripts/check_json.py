"""Structural check on an ISO 20022 JSON transaction export.

The original version read only `data[0]['Ntry']` and silently ignored every
other element of the list, so a file with ten statements was validated on one.
It also resolved its path by walking two directories up from __file__, which
broke if the file moved.

    python scripts/check_json.py
    python scripts/check_json.py --file data/transactions.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

REQUIRED_ENTRY_FIELDS = ("NtryRef", "Amt", "CdtDbtInd")


def validate(path: Path) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "path": str(path), "valid": False, "statements": 0,
        "entries": 0, "errors": [], "warnings": [],
    }

    if not path.exists():
        report["errors"].append(f"File not found: {path}")
        return report

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report["errors"].append(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
        return report

    statements: List[dict] = data if isinstance(data, list) else [data]
    if not statements:
        report["errors"].append("Document contains no statements.")
        return report

    report["statements"] = len(statements)

    for s_idx, statement in enumerate(statements):
        if not isinstance(statement, dict):
            report["errors"].append(f"Statement {s_idx}: expected an object, got {type(statement).__name__}.")
            continue

        entries = statement.get("Ntry")
        if entries is None:
            report["errors"].append(f"Statement {s_idx}: missing the 'Ntry' collection.")
            continue
        if not isinstance(entries, list):
            report["errors"].append(f"Statement {s_idx}: 'Ntry' must be a list.")
            continue
        if not entries:
            report["warnings"].append(f"Statement {s_idx}: no entries.")
            continue

        report["entries"] += len(entries)
        for e_idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                report["errors"].append(f"Statement {s_idx}, entry {e_idx}: not an object.")
                continue
            missing = [f for f in REQUIRED_ENTRY_FIELDS if f not in entry]
            if missing:
                report["warnings"].append(
                    f"Statement {s_idx}, entry {e_idx}: missing {', '.join(missing)}."
                )

    report["valid"] = not report["errors"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=config.DATA_DIR / "transactions.json")
    args = parser.parse_args()

    report = validate(args.file)
    status = "VALID" if report["valid"] else "FAILED"
    print(f"\nISO 20022 structure: {status}")
    print(f"Statements: {report['statements']}   Entries: {report['entries']}")

    for error in report["errors"]:
        print(f"  ERROR   {error}")
    for warning in report["warnings"][:20]:
        print(f"  WARN    {warning}")
    if len(report["warnings"]) > 20:
        print(f"  ... and {len(report['warnings']) - 20} more warnings")

    print()
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
