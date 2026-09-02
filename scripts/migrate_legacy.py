"""One-off, repeatable migration helper from the two legacy local projects.

The script intentionally copies source/config/tests/docs only. Secrets, logs,
build artifacts, node_modules and large business datasets are excluded.
"""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT.parent / "andian"
CONTROL_SOURCE = LEGACY / "Agent-Platform" / "agent-platform"
RUNTIME_SOURCE = LEGACY / "agentic rag"

CONTROL_TARGET = ROOT / "apps" / "control-plane"
RUNTIME_TARGET = ROOT / "services" / "runtime"

IGNORED_NAMES = {
    ".git",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "logs",
    ".gradio",
    ".DS_Store",
    ".env",
    ".env.local",
}


def ignore(_: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_NAMES or name.endswith(".log")}


def copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)


def replace_text(root: Path, old: str, new: str) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".py", ".md", ".txt", ".toml", ".yaml", ".yml"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old in content:
            path.write_text(content.replace(old, new), encoding="utf-8")


def migrate_control_plane() -> None:
    # Copy the whole application shell, excluding local secrets/build outputs.
    copy_tree(CONTROL_SOURCE / "backend", CONTROL_TARGET / "backend")
    copy_tree(CONTROL_SOURCE / "frontend" / "src", CONTROL_TARGET / "frontend" / "src")
    for filename in ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts"):
        source = CONTROL_SOURCE / "frontend" / filename
        if source.exists():
            target = CONTROL_TARGET / "frontend" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    copy_tree(CONTROL_SOURCE / "docs", CONTROL_TARGET / "docs")


def migrate_runtime() -> None:
    for dirname in ("apps", "config", "docs", "evals", "integrations", "observability", "scripts", "tests"):
        source = RUNTIME_SOURCE / dirname
        if source.exists():
            copy_tree(source, RUNTIME_TARGET / dirname)

    source_pkg = RUNTIME_SOURCE / "src" / "agentic_rag"
    target_pkg = RUNTIME_TARGET / "src" / "aegora_runtime"
    copy_tree(source_pkg, target_pkg)

    for filename in ("requirements.txt", "requirements.docker.txt", "docker-entrypoint.sh"):
        source = RUNTIME_SOURCE / filename
        if source.exists():
            shutil.copy2(source, RUNTIME_TARGET / filename)

    # Keep only safe example Skill assets, not historical customer/session dumps.
    skill_source = RUNTIME_SOURCE / "data" / "skills"
    if skill_source.exists():
        copy_tree(skill_source, RUNTIME_TARGET / "data" / "skills")

    replace_text(RUNTIME_TARGET, "agentic_rag", "aegora_runtime")
    replace_text(RUNTIME_TARGET, "Agentic RAG", "Aegora Runtime")
    # Scrub machine-specific paths embedded in historical evaluation artifacts.
    replace_text(RUNTIME_TARGET, "/Users/ann/app_demo/agentic rag", "<runtime-root>")


def main() -> None:
    migrate_control_plane()
    migrate_runtime()
    print(f"Migrated Control Plane -> {CONTROL_TARGET}")
    print(f"Migrated Runtime       -> {RUNTIME_TARGET}")


if __name__ == "__main__":
    main()

