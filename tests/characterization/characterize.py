#!/usr/bin/env python3
"""Record and verify golden fixtures of the proxy's wire behaviour.

    # record, against a reference app tree
    ./characterize.py record --app-path /path/to/app-tree

    # verify, against this repo's own app tree
    ./characterize.py verify --app-path ../../pii_redact_proxy

`--app-path` exists because the goldens should predate a refactor: a fixture
recorded from the refactored code proves only that the refactor agrees with
itself. Record from the tree before the change, verify against the tree after.

Each case runs in its own subprocess. The three apps import overlapping module
names (`streaming` vs `openai_streaming`, and each app's own `redactor` stub),
so sharing an interpreter would let one case's imports decide another's
behaviour — the exact class of accident these fixtures are meant to catch.
"""
import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def _run_child(app_path, case_name):
    proc = subprocess.run(
        [sys.executable, str(HERE / "characterize.py"), "_run",
         "--app-path", str(app_path), "--case", case_name],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{case_name}: recorder failed\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _select(args):
    sys.path.insert(0, str(HERE))
    import cases
    chosen = cases.by_provider(args.provider)
    if args.case:
        chosen = [c for c in chosen if c["name"] == args.case]
        if not chosen:
            sys.exit(f"no case named {args.case}")
    return chosen


def cmd_record(args):
    sys.path.insert(0, str(HERE))
    import harness
    for case in _select(args):
        rec = _run_child(args.app_path, case["name"])
        p = harness.write_fixture(FIXTURES, rec)
        print(f"recorded  {case['name']:<55} -> {p.name}")


def cmd_verify(args):
    sys.path.insert(0, str(HERE))
    import harness
    failures = 0
    for case in _select(args):
        name = case["name"]
        path = harness.fixture_path(FIXTURES, name)
        if not path.exists():
            print(f"MISSING   {name}  (no fixture; record it first)")
            failures += 1
            continue
        actual = _run_child(args.app_path, name)
        differences = harness.diff(harness.read_fixture(FIXTURES, name), actual)
        if differences:
            failures += 1
            print(f"DIFF      {name}")
            for d in differences:
                print(f"            {d}")
        else:
            print(f"ok        {name}")
    print(f"\n{len(_select(args))} cases, {failures} differing")
    return 1 if failures else 0


def cmd_run(args):
    """Internal: record one case and print it. Not meant to be called directly."""
    sys.path.insert(0, str(HERE))
    import cases
    import harness
    case = next(c for c in cases.CASES if c["name"] == args.case)
    json.dump(harness.run_case(args.app_path, case), sys.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("record", "verify", "_run"):
        p = sub.add_parser(name)
        p.add_argument("--app-path", required=True,
                       help="directory holding main.py / openai_main.py / chatgpt_main.py")
        p.add_argument("--provider", help="anthropic | openai | chatgpt")
        p.add_argument("--case", help="a single case name")
    args = ap.parse_args()
    sys.exit({"record": cmd_record, "verify": cmd_verify,
              "_run": cmd_run}[args.cmd](args) or 0)


if __name__ == "__main__":
    main()
