"""Fast repository checks that do not require service dependencies or databases."""

from __future__ import annotations

import compileall
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"[FAIL] {message}")


def check_no_nested_secret_files() -> None:
    nested = [
        path
        for path in ROOT.rglob(".env")
        if path.parent != ROOT and ".git" not in path.parts
    ]
    if nested:
        fail("nested .env files found: " + ", ".join(str(path.relative_to(ROOT)) for path in nested))


def check_no_legacy_runtime_imports() -> None:
    offenders = []
    for path in [ROOT / "services" / "runtime" / "src", ROOT / "services" / "runtime" / "apps"]:
        for source in path.rglob("*.py"):
            if "agentic_rag" in source.read_text(encoding="utf-8"):
                offenders.append(source.relative_to(ROOT))
    if offenders:
        fail("legacy runtime imports found: " + ", ".join(map(str, offenders)))


def check_contracts() -> None:
    for path in (ROOT / "packages" / "contracts").glob("*.schema.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail(f"unsupported contract schema version: {path.name}")


def check_compile() -> None:
    targets = [
        ROOT / "apps" / "control-plane" / "backend" / "app",
        ROOT / "services" / "runtime" / "src" / "aegora_runtime",
        ROOT / "services" / "runtime" / "apps",
    ]
    for target in targets:
        if not compileall.compile_dir(target, quiet=1):
            fail(f"compile failed: {target.relative_to(ROOT)}")


def main() -> None:
    check_no_nested_secret_files()
    check_no_legacy_runtime_imports()
    check_contracts()
    check_compile()
    print("[OK] Aegora migration checks passed")


if __name__ == "__main__":
    main()

