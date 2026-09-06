#!/usr/bin/env python3
"""Stage Side Dog's canonical Markdown as a Jekyll Pages source tree."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SECURITY = ROOT / "SECURITY.md"
DOCS = ROOT / "docs"
SITE = DOCS / "site"
DEFAULT_OUTPUT = ROOT / "_pages-source"
COPIED_SUFFIXES = {".md", ".png", ".gif", ".jpg", ".jpeg", ".svg", ".webp"}


def _front_matter(title: str, permalink: str) -> str:
    safe_title = title.replace('"', '\\"')
    return (
        '---\n'
        'layout: default\n'
        f'title: "{safe_title}"\n'
        f'permalink: "{permalink}"\n'
        '---\n\n'
    )


def _markdown_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _write_markdown(
    source: Path,
    destination: Path,
    *,
    permalink: str,
    title: str | None = None,
) -> None:
    body = source.read_text(encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        _front_matter(
            title or _markdown_title(body, source.stem.replace("-", " ").title()),
            permalink,
        )
        + body,
        encoding="utf-8",
    )


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    """Create a disposable Pages source tree and return its path."""
    output = output.resolve()
    if output.name != "_pages-source":
        raise ValueError("the generated directory must be named _pages-source")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    for source in SITE.rglob("*"):
        if source.is_file():
            destination = output / source.relative_to(SITE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    _write_markdown(README, output / "index.md", permalink="/", title="Side Dog")
    # README links to SECURITY.md; stage it as a page so jekyll-relative-links
    # can rewrite that link and the rendered-site link check finds a target.
    _write_markdown(SECURITY, output / "SECURITY.md", permalink="/security/")
    shutil.copy2(ROOT / "LICENSE", output / "LICENSE")

    for source in DOCS.rglob("*"):
        if not source.is_file() or SITE in source.parents:
            continue
        if source.suffix.lower() not in COPIED_SUFFIXES:
            continue
        destination = output / "docs" / source.relative_to(DOCS)
        if source.suffix.lower() == ".md":
            relative_url = source.relative_to(DOCS).with_suffix("").as_posix()
            _write_markdown(source, destination, permalink=f"/docs/{relative_url}/")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
