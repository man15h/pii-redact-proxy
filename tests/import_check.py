"""Import every module in the package, with only the heavy externals stubbed.

The gate this closes: the characterization fixtures stub `redactor` and
`session` wholesale, so the import lines inside those files never execute in
any test. A flat `from recognizers import ...` in `core/redactor.py` —
correct in a flat app directory, unresolvable inside a package — would pass
every fixture and crash-loop the container on exit 1 the first time it ran.

So this stubs presidio and nothing else. Every module of `pii_redact_proxy` is
imported for real, which means every import statement in the package runs, and
`core/redactor.py`'s import-time setup runs with it.

Not a substitute for the image smoke test in the Dockerfile, which does the
same thing with the real dependencies. This one is the cheap early copy: it
needs no build, so it fails in the fixtures job seconds after a push instead of
minutes later.
"""
import pathlib
import pkgutil
import sys
import tempfile
from unittest.mock import MagicMock

# config.yml is not in this repo on purpose (it lists what we redact), but
# core/redactor.py calls load_config() at import time. An empty-but-valid one
# satisfies that without inventing redaction targets: the recognizers built
# from it are empty lists, which is all this check needs.
CONFIG = "domains: []\nhostnames: []\nusernames: []\npaths: []\nstatic_mappings: {}\n"

# Presidio pulls in spaCy and a language model — minutes of install for a check
# about import resolution. These four names are what the package imports from.
PRESIDIO = (
    "presidio_analyzer",
    "presidio_analyzer.nlp_engine",
    "presidio_anonymizer",
    "presidio_anonymizer.entities",
)


def _package_module_names(modules: list[str]) -> set[str]:
    """Bare names of the package's own modules, e.g. {"recognizers", "session"}.

    A flat `from recognizers import ...` raises ModuleNotFoundError with
    name="recognizers", which is a package defect and must not be excused as a
    missing dependency — that is the exact bug this check exists to catch.
    """
    return {name.rsplit(".", 1)[-1] for name in modules}


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in PRESIDIO:
        sys.modules[name] = MagicMock()

    sys.path.insert(0, str(root))
    import pii_redact_proxy

    modules = [
        name
        for _, name, _ in pkgutil.walk_packages(
            pii_redact_proxy.__path__, prefix="pii_redact_proxy."
        )
    ]
    if not modules:
        print("FAIL: walk_packages found no modules — is the package laid out as expected?")
        return 1

    failed = []
    for name in sorted(modules):
        try:
            __import__(name)
        except Exception as exc:
            # A missing third-party module is this venv being short a
            # dependency, not the package being broken. Both fail the run —
            # the check cannot do its job either way — but they are fixed in
            # different files, so say which one this is. The first version of
            # this script did not, and reported "1 failed to import" when the
            # only problem was that uvicorn was not installed.
            missing = getattr(exc, "name", "") or ""
            external = isinstance(exc, ModuleNotFoundError) and not missing.startswith(
                "pii_redact_proxy"
            ) and missing not in _package_module_names(modules)
            hint = (
                f"\n            (not a package defect: install {missing!r} in this venv"
                " or add it to PRESIDIO if it should be stubbed)"
                if external
                else ""
            )
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL      {name}\n            {type(exc).__name__}: {exc}{hint}")
        else:
            print(f"ok        {name}")

    print(f"\n{len(modules)} modules, {len(failed)} failed to import")
    return 1 if failed else 0


if __name__ == "__main__":
    # Run from a scratch directory so the throwaway config.yml is never written
    # next to the real tree, and load_config() finds it at its default path.
    with tempfile.TemporaryDirectory() as tmp:
        (pathlib.Path(tmp) / "config.yml").write_text(CONFIG)
        import os
        os.chdir(tmp)
        sys.exit(main())
