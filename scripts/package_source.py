"""Build a GitHub-ready source ZIP without local environments or credentials."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {".git", ".venv", ".tools", ".runtime", ".runs", ".config", "__pycache__",
           ".pytest_cache", ".ruff_cache", ".bootstrap", "node_modules", "dist", "results", ".vscode"}
TOP_LEVEL = {"README.md", "LICENSE", ".gitignore", ".env.example", "start.cmd", "install.ps1", "start-canvas.ps1",
             "config", "scripts", "mini-swe-agent", "experiments", "tests", ".github"}


def source_files(root: Path):
    for top in sorted(root.iterdir()):
        if top.name not in TOP_LEVEL:
            continue
        pending = [top]
        while pending:
            path = pending.pop()
            if path.relative_to(root).as_posix() == "scripts/smoke_check.py":
                continue  # Retired DeepAgents script; it is not part of the OMPy runtime.
            if path.is_symlink() or path.name in EXCLUDE or path.name.endswith(".egg-info"):
                continue
            if path.is_dir():
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif (path.name != ".env" and not path.name.startswith(".env.")) or path.name == ".env.example":
                if path.suffix not in {".pyc", ".pyo"} and (path.suffix != ".log" or "evidence" in path.parts):
                    yield path


def package(root: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for path in source_files(root):
            archive.write(path, "OMPy/" + path.relative_to(root).as_posix())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "OMPy-source.zip")
    args = parser.parse_args()
    package(ROOT, args.output.resolve())
    print(args.output.resolve())
