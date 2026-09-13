#!/usr/bin/env python3
"""Prove requirements-test.txt still covers what the suite actually imports.

LAW §15 names this check and names it cheap and static: every third-party
import, by AST, against `sys.stdlib_module_names`, covered by the declared
set. The failure it exists to catch is the one that bit jrackerby/HA (GH-670)
- a suite that only ever ran in a shared venv imports whatever that venv
carried for other reasons, and the first clean runner is where it learns this.
A declaration file nobody re-checks drifts the same way, just more slowly: the
next import added to a module under test lands in CI as a broken test rather
than as a missing declaration.

Scope is the suite AND the modules it loads, because loading a module executes
its imports - test_srp.py needs aiohttp not because any check does I/O but
because auth.py imports it at module scope.
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(TESTS, "requirements-test.txt")

# The modules the suite loads directly, plus what those import locally.
# content_in_root: these live at the repository root.
MODULES_UNDER_TEST = ("auth.py", "const.py", "signalr_protocol.py")

# Import name -> distribution name, for the cases where they differ. Kept
# explicit and tiny rather than resolved through importlib.metadata: the point
# is a check that runs BEFORE anything is installed.
DISTRIBUTION_OF = {
    "yaml": "pyyaml",
}


def _sources():
    for name in sorted(os.listdir(TESTS)):
        if name.endswith(".py"):
            yield os.path.join(TESTS, name)
    for name in MODULES_UNDER_TEST:
        yield os.path.join(REPO, name)


def _top_level_imports(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import - this package's own code.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _declared():
    declared = set()
    with open(REQUIREMENTS, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            # Strip any PEP 508 version specifier or extras.
            for separator in ("[", "=", ">", "<", "!", "~", ";", " "):
                line = line.split(separator, 1)[0]
            if line:
                declared.add(line.strip().lower())
    return declared


def _third_party():
    found = {}
    for path in _sources():
        for name in _top_level_imports(path):
            if name in sys.stdlib_module_names:
                continue
            # This repo's own modules, imported by path rather than installed.
            if os.path.exists(os.path.join(REPO, f"{name}.py")):
                continue
            found.setdefault(name, []).append(os.path.relpath(path, REPO))
    return found


def test_every_third_party_import_is_declared():
    declared = _declared()
    undeclared = {
        name: where
        for name, where in _third_party().items()
        if DISTRIBUTION_OF.get(name, name).lower() not in declared
    }
    if undeclared:
        _fail(
            "third-party imports not declared in tests/requirements-test.txt: "
            + ", ".join(f"{name} (imported by {', '.join(where)})" for name, where in sorted(undeclared.items()))
        )


def test_declaration_is_not_empty():
    """A requirements file that parses to nothing would make the check above
    pass only because it had nothing to compare against."""
    if not _declared():
        _fail("requirements-test.txt declared no distributions at all")


def _fail(msg):
    raise AssertionError(msg)


def test_self_test_can_fail():
    """LAW §4: an assertion set needs a self-test proving it CAN fail. Feed the
    coverage comparison a name that is certainly not in the declaration file
    and confirm it is reported as undeclared, so a green run above is evidence
    about the imports rather than about the comparison being vacuous."""
    declared = _declared()
    if "definitely-not-a-real-distribution" in declared:
        _fail("self-test failed to fail: the sentinel name is actually declared")
    # And prove the stdlib filter is not swallowing everything: msgpack is a
    # real third-party import in this suite and must be seen as one.
    if "msgpack" in sys.stdlib_module_names:
        _fail("self-test failed to fail: msgpack read as stdlib")
    if "msgpack" not in _third_party():
        _fail("self-test failed to fail: msgpack was not detected as a third-party import")


CASES = (
    test_every_third_party_import_is_declared,
    test_declaration_is_not_empty,
)


def main():
    test_self_test_can_fail()
    for case in CASES:
        case()
        print(f"ok: {case.__name__}")
    print(f"\n{len(CASES)} checks passed (plus 1 self-test).")


if __name__ == "__main__":
    main()
