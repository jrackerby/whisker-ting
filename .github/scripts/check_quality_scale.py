#!/usr/bin/env python3
"""Check quality_scale.yaml's row set against Home Assistant's own rule list.

WHY THIS EXISTS. hassfest never reads a custom component's quality_scale.yaml
-- `validate_iqs_file` returns immediately unless `integration.core` -- so the
gap list in this repo has no gate behind it and would drift out of date with
the standard the moment core adds, renames or retires a rule. Nothing would
say so. This is that gate.

It reads the rule names and tiers from `ALL_RULES` in home-assistant/core's
`script/hassfest/quality_scale.py`, by AST rather than by regex, and asserts
that quality_scale.yaml carries exactly those rows with valid statuses -- the
same shape hassfest's own SCHEMA enforces for a core integration.

A FETCH THAT FAILS IS A VOID, NOT A PASS. If core's file cannot be read, this
exits non-zero saying it could not run, rather than reporting green over a
comparison it never made.

Usage:
    python3 .github/scripts/check_quality_scale.py [--ref dev] [--file quality_scale.yaml]
    python3 .github/scripts/check_quality_scale.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import urllib.error
import urllib.request

import yaml

RULES_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/"
    "{ref}/script/hassfest/quality_scale.py"
)
VALID_STATUSES = {"todo", "done", "exempt"}
# A bare string row is only legal for these two; `exempt` must carry a reason,
# which is hassfest's rule and not this script's preference.
STATUSES_NEEDING_NO_COMMENT = {"todo", "done"}


def fetch_all_rules(ref: str) -> dict[str, str]:
    """Return {rule_name: tier} read from core's ALL_RULES by AST."""
    url = RULES_URL.format(ref=ref)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            source = response.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SystemExit(f"CANNOT RUN: could not read {url}: {err}")

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "ALL_RULES" for t in node.targets
        ):
            continue

        rules: dict[str, str] = {}
        for element in node.value.elts:  # type: ignore[attr-defined]
            name = element.args[0].value
            tier = element.args[1].attr
            rules[name] = tier
        if not rules:
            raise SystemExit("CANNOT RUN: ALL_RULES parsed to zero rules")
        return rules

    raise SystemExit("CANNOT RUN: no ALL_RULES assignment in core's quality_scale.py")


def check(rows: dict, expected: dict[str, str]) -> list[str]:
    """Return a list of failures. Empty means the file matches the standard."""
    failures: list[str] = []

    missing = sorted(set(expected) - set(rows))
    if missing:
        failures.append(f"rules in the standard but not in the file: {missing}")

    unknown = sorted(set(rows) - set(expected))
    if unknown:
        failures.append(f"rules in the file that the standard does not define: {unknown}")

    for name in sorted(set(rows) & set(expected)):
        value = rows[name]
        if isinstance(value, str):
            if value not in STATUSES_NEEDING_NO_COMMENT:
                failures.append(
                    f"{name}: bare status {value!r} is not one of "
                    f"{sorted(STATUSES_NEEDING_NO_COMMENT)}"
                )
            continue
        if not isinstance(value, dict):
            failures.append(f"{name}: expected a status string or a mapping, got {type(value).__name__}")
            continue
        status = value.get("status")
        if status not in VALID_STATUSES:
            failures.append(f"{name}: status {status!r} is not one of {sorted(VALID_STATUSES)}")
        if not str(value.get("comment", "")).strip():
            failures.append(f"{name}: a mapping row must carry a non-empty comment")

    return failures


def self_test(expected: dict[str, str]) -> None:
    """Prove the check CAN fail. A gate never seen to fail is not evidence."""
    good = {name: "todo" for name in expected}
    assert not check(good, expected), "self-test: a complete todo file should pass"

    cases = {
        "a dropped rule is caught": {k: v for k, v in list(good.items())[1:]},
        "an invented rule is caught": {**good, "not-a-real-rule": "todo"},
        "a bad status is caught": {**good, sorted(good)[0]: "partly"},
        "a bare exempt is caught": {**good, sorted(good)[0]: "exempt"},
        "an exempt with no comment is caught": {
            **good,
            sorted(good)[0]: {"status": "exempt", "comment": "  "},
        },
    }
    for label, rows in cases.items():
        failures = check(rows, expected)
        assert failures, f"self-test FAILED to fail: {label}"
        print(f"  self-test ok: {label} -> {failures[0]}")

    print(f"self-test passed against {len(expected)} live rules")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="dev", help="home-assistant/core ref to read the rules from")
    parser.add_argument("--file", default="quality_scale.yaml")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    expected = fetch_all_rules(args.ref)
    print(f"read {len(expected)} rules from home-assistant/core@{args.ref}")

    if args.self_test:
        self_test(expected)
        return 0

    path = pathlib.Path(args.file)
    if not path.is_file():
        print(f"FAIL: {path} does not exist")
        return 1

    document = yaml.safe_load(path.read_text())
    rows = (document or {}).get("rules")
    if not isinstance(rows, dict):
        print(f"FAIL: {path} has no `rules` mapping")
        return 1

    failures = check(rows, expected)
    if failures:
        print(f"FAIL: {path} does not match the standard")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    tally: dict[str, int] = {}
    for value in rows.values():
        status = value if isinstance(value, str) else value.get("status")
        tally[status] = tally.get(status, 0) + 1
    print(f"OK: {len(rows)} rows, all defined by the standard -- {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
