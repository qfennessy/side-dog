#!/usr/bin/env python3
"""Check canonical Markdown links and rendered Pages links without the network."""

from __future__ import annotations

import argparse
import html.parser
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)\s]+)(?:\s+[\"'][^)]*[\"'])?\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
EXTERNAL_SCHEMES = {"http", "https", "mailto"}
REQUIRED_README_HEADINGS = {
    "install",
    "try-it",
    "coding-agent-support",
    "with-or-without-herdr",
    "what-side-dog-shows",
    "terminal-and-panel-controls",
    "configuration",
    "other-commands",
}


def slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^a-z0-9 _-]", "", text)
    return re.sub(r"[ _]+", "-", text).strip("-")


def check_markdown(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    files = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    files = [path for path in files if "site" not in path.relative_to(root).parts]
    readme_headings: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        headings = {slug(match.group(1)) for match in map(HEADING.match, text.splitlines()) if match}
        if path == root / "README.md":
            readme_headings = headings
        for target in MARKDOWN_LINK.findall(text):
            parsed = urlsplit(target)
            if parsed.scheme in EXTERNAL_SCHEMES or target.startswith("#"):
                if target.startswith("#") and unquote(target[1:]) not in headings:
                    errors.append(f"{path.relative_to(root)}: missing anchor {target}")
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.exists():
                errors.append(f"{path.relative_to(root)}: missing target {target}")
    missing = REQUIRED_README_HEADINGS - readme_headings
    if missing:
        errors.append("README.md: missing documentation sections: " + ", ".join(sorted(missing)))
    return errors


class _Links(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.targets: list[str] = []
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.anchors.add(values["id"] or "")
        attribute = "href" if tag == "a" else "src" if tag in {"img", "script"} else None
        if attribute and values.get(attribute):
            self.targets.append(values[attribute] or "")


def _rendered_destination(site: Path, current: Path, path: str, base_path: str) -> Path:
    decoded = unquote(path)
    if decoded.startswith(base_path + "/") or decoded == base_path:
        decoded = decoded[len(base_path):]
    if decoded.startswith("/"):
        candidate = site / decoded.lstrip("/")
    else:
        candidate = current.parent / decoded
    if decoded.endswith("/") or candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate


def check_rendered(site: Path, *, base_path: str = "/side-dog") -> list[str]:
    errors: list[str] = []
    pages: dict[Path, _Links] = {}
    for path in site.rglob("*.html"):
        parser = _Links()
        parser.feed(path.read_text(encoding="utf-8"))
        pages[path.resolve()] = parser
    if not pages:
        return [f"{site}: no rendered HTML files"]
    for current, parsed_page in pages.items():
        for target in parsed_page.targets:
            parsed = urlsplit(target)
            if parsed.scheme in EXTERNAL_SCHEMES or target.startswith("mailto:"):
                continue
            destination = _rendered_destination(site.resolve(), current, parsed.path, base_path)
            if not destination.exists():
                errors.append(f"{current.relative_to(site.resolve())}: missing target {target}")
                continue
            if parsed.fragment and destination.suffix == ".html":
                destination_page = pages.get(destination.resolve())
                if destination_page and unquote(parsed.fragment) not in destination_page.anchors:
                    errors.append(f"{current.relative_to(site.resolve())}: missing anchor {target}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered", type=Path)
    parser.add_argument("--base-path", default="/side-dog")
    args = parser.parse_args()
    errors = check_rendered(args.rendered, base_path=args.base_path) if args.rendered else check_markdown()
    if errors:
        raise SystemExit("\n".join(errors))
    print("Documentation links are valid.")


if __name__ == "__main__":
    main()
