# TOOLS

Traps for the instruments THIS repo owns. Per-repo by CLAUDE.md §0 / GH-676:
`jrackerby/HA`'s TOOLS.md covers the Home Assistant instrument surface and this
one covers what bites here. A line may live in one file or the other, never
both. Every line is a claim with a timestamp — re-verify before building a plan
on one, and edit it when it stops being true.

## pytest against a `content_in_root` layout

- **RUN THE SUITE FROM INSIDE `tests/`, never from the repository root.**
  `hacs.json` sets `content_in_root`, so `__init__.py` sits at the root and the
  repository root IS a Python package. pytest turns any collected directory
  carrying an `__init__.py` into a `Package` node and **imports it during
  setup**, so a run rooted at the repo imports the integration's `__init__.py`,
  which imports `homeassistant` — 20 collection errors that say nothing about
  the code and name a `SyntaxError` or `ModuleNotFoundError` in a file the
  suite never meant to touch. `cd tests && python3 -m pytest . -q` is green.
  **`--import-mode=importlib` does NOT avoid it** — measured; only starting
  collection below the root does. Each test file also still runs standalone
  from the root (`python3 tests/test_srp.py`), which is the quickest way to
  tell this trap apart from a real failure.
- **The suite needs Python 3.12+.** `__init__.py` uses a PEP 695 `type`
  statement, and pytest imports it via the trap above even when collection
  succeeds. On 3.11 the failure presents as `SyntaxError: invalid syntax` at
  `type WhiskerTingConfigEntry = ...`, which reads like broken source.

## hassfest and HACS against this repo

- hassfest takes no path input (`jrackerby/HA` `tools/work_docs/TOOLS.md`,
  "Landing a branch"): `validate.yml` checks out to `src/`, stages the layout
  and asserts the staged `manifest.json` exists.
- HACS reaches only a public repo (`jrackerby/HA` LAW §15); `validate.yml`
  reports the private-repo 404 as a VOID, not a pass.
- `brand/` is singular and belongs at the repository root here, where
  `content_in_root` copies it to the one path both HACS's `brands` validator
  and core read (`jrackerby/HA` `tools/work_docs/TOOLS.md` carries the trap).

## Releases

- **`release.yml` fires on a push to `master` that touches `manifest.json`**,
  and tags from that file's own `version`. It is idempotent on the TAG, not on
  the diff: a push editing a URL and leaving the version alone re-runs the
  workflow and releases nothing. A version bump that never reaches `master`
  cuts no release, so HACS never offers the upgrade.
