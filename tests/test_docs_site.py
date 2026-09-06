from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocumentationSiteTests(unittest.TestCase):
    def test_stages_readme_and_docs_as_canonical_page_bodies(self) -> None:
        builder = load_script("build_docs_site.py")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "_pages-source"
            builder.build(output)
            index = (output / "index.md").read_text(encoding="utf-8")
            self.assertEqual(index.split("---\n\n", 1)[1], (ROOT / "README.md").read_text(encoding="utf-8"))
            releasing = (output / "docs" / "releasing.md").read_text(encoding="utf-8")
            self.assertIn('permalink: "/docs/releasing/"', releasing)
            self.assertEqual(releasing.split("---\n\n", 1)[1], (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8"))
            self.assertTrue((output / "docs" / "side-dog-logo.png").is_file())
            security = (output / "SECURITY.md").read_text(encoding="utf-8")
            self.assertIn('permalink: "/security/"', security)
            self.assertEqual(security.split("---\n\n", 1)[1], (ROOT / "SECURITY.md").read_text(encoding="utf-8"))

    def test_canonical_documentation_has_valid_local_links_and_required_sections(self) -> None:
        checker = load_script("check_docs_links.py")
        self.assertEqual(checker.check_markdown(ROOT), [])

    def test_pages_workflow_limits_publish_permissions_to_deploy_job(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        build, deploy = workflow.split("\n  deploy:\n", 1)
        self.assertNotIn("pages: write", build)
        self.assertNotIn("id-token: write", build)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)
        self.assertIn("actions/jekyll-build-pages@", workflow)
        # Every file staged into the site must retrigger the workflow when it
        # changes, or the published site silently falls behind the repository.
        for staged in ("README.md", "LICENSE", "SECURITY.md", "docs/**"):
            self.assertEqual(workflow.count(f"      - {staged}\n"), 2, staged)
        self.assertIn("actions/deploy-pages@", workflow)


if __name__ == "__main__":
    unittest.main()
