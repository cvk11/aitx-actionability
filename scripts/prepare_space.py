#!/usr/bin/env python
"""Assemble a Hugging Face Space folder (outputs/space_bundle/) WITHOUT deploying it.
Deployment requires the owner's explicit authorization (see deployment/README.md)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    out = ROOT / "outputs" / "space_bundle"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "src", out / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    shutil.copytree(ROOT / "prompts", out / "prompts")
    (out / "data" / "official").mkdir(parents=True)
    shutil.copy(ROOT / "data" / "official" / "aitxchallenge_report_template.md", out / "data" / "official")
    (out / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "build_genereviews_index.py", out / "scripts")
    shutil.copy(ROOT / "deployment" / "app.py", out / "app.py")
    shutil.copy(ROOT / "deployment" / "requirements.txt", out / "requirements.txt")
    shutil.copy(ROOT / "deployment" / "SPACE_README.md", out / "README.md")
    (out / ".gitignore").write_text(".env\ndata/genereviews/\ndata/cache/\n__pycache__/\n")
    leaked = [p for p in out.rglob("*") if p.is_file() and (p.name == ".env" or "official_cases" in str(p))]
    if leaked:
        print("refusing: bundle contains", leaked)
        return 1
    print(f"Space bundle ready at {out} (not deployed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
