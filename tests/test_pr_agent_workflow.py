"""Execute the actual workflow shell guards with synthetic GitHub responses."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/pr-agent.yml").read_text()
MARKER = "<!-- pr-agent:review:full -->"


def step(name):
    return WORKFLOW.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]


class WorkflowContractTest(unittest.TestCase):
    def test_automatic_reviews_target_main_and_skip_dependabot(self):
        self.assertIn("pull_request:\n    branches: [main]", WORKFLOW)
        job_gate = WORKFLOW.split("    if: |", 1)[1].split("    runs-on:", 1)[0]
        self.assertIn("github.actor != 'dependabot[bot]'", job_gate)
        self.assertIn("github.event.pull_request.user.login != 'dependabot[bot]'", job_gate)

    def test_lockfile_classification_precedes_all_provider_requests(self):
        self.assertLess(WORKFLOW.index("- name: Check for files PR-Agent will review"),
                        WORKFLOW.index("- name: Verify OpenAI authentication"))
        self.assertIn("steps.files.outputs.reviewable == 'true'",
                      step("Verify OpenAI authentication"))

    def test_trusted_base_and_fork_gates_precede_credentials(self):
        self.assertNotIn("pull_request_target:", WORKFLOW)
        self.assertNotIn("base.ref != 'main'", WORKFLOW)
        self.assertIn("!github.event.pull_request.head.repo.fork", WORKFLOW)
        self.assertIn('"OWNER", "MEMBER", "COLLABORATOR"', WORKFLOW)
        self.assertIn("github.event.issue.pull_request != null", WORKFLOW)
        checkout = step("Check out the source-pinned action from the trusted base revision")
        self.assertIn("github.event.pull_request.base.sha || github.event.repository.default_branch", checkout)
        self.assertNotIn("head.sha", checkout)
        self.assertIn("persist-credentials: false", checkout)
        self.assertRegex(checkout, r"uses: actions/checkout@[a-f0-9]{40}")
        action = step("PR Agent action step")
        for guarded in (checkout, action, step("Verify OpenAI authentication")):
            self.assertIn("steps.head.outputs.head_is_fork == 'false'", guarded)
            self.assertIn("steps.files.outputs.reviewable == 'true'", guarded)
        self.assertIn("uses: ./.github/actions/pr-agent", action)
        self.assertIn("openai__key: ${{ secrets.OPENAI_API_KEY || secrets.OPENROUTER_API_KEY }}", action)
        self.assertNotIn("https://openrouter.ai", WORKFLOW)
        self.assertNotIn("openrouter__", WORKFLOW)
        self.assertIn("steps.files.outputs.reviewable != 'false'", step("Record review verdict"))

    def test_workflow_and_config_agree_on_bounded_single_reviewer(self):
        config = tomllib.loads((ROOT / ".pr_agent.toml").read_text())["config"]
        self.assertIn("REVIEWER_MODEL: " + config["model"], WORKFLOW)
        self.assertIn(json.dumps(config["fallback_models"]), WORKFLOW)
        self.assertNotIn("matrix:", WORKFLOW)
        self.assertIn("timeout-minutes: 10", WORKFLOW)
        for name in ("ai_timeout", "reasoning_effort", "num_retries", "retry_same_model_on_timeout", "output_run_details"):
            value = str(config[name]).lower() if isinstance(config[name], bool) else str(config[name])
            self.assertIn(f'config.{name}: "{value}"', WORKFLOW)


@unittest.skipUnless(shutil.which("jq"), "jq required for workflow shell guards")
class WorkflowGuardTest(unittest.TestCase):
    def run_step(self, name, response, **overrides):
        script = textwrap.dedent(step(name).split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / "gh"
            gh.write_text('#!/bin/sh\nprintf "%s" "$FIXTURE_RESPONSE"\nexit "${FIXTURE_EXIT:-0}"\n')
            gh.chmod(0o700)
            curl = root / "curl"
            curl.write_text('#!/bin/sh\nprintf "%s" "${FIXTURE_STATUS:-200}"\n')
            curl.chmod(0o700)
            summary, output = root / "summary", root / "output"
            summary.touch()
            output.touch()
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                   "FIXTURE_RESPONSE": response, "GITHUB_REPOSITORY": "qfennessy/side-dog",
                   "PR_NUMBER": "1", "GITHUB_STEP_SUMMARY": str(summary),
                   "GITHUB_OUTPUT": str(output), "STARTED_AT": "2026-09-08T10:00:00Z",
                   "PR_AGENT_OUTCOME": "success", "REVIEWER_ID": "luna",
                   "REVIEWER_PROVIDER": "openai", "REVIEWER_API_BASE": "https://api.openai.com/v1",
                   "REVIEWER_MODEL": "gpt-5.6-luna", **overrides}
            result = subprocess.run(["bash", "-c", script], env=env, text=True,
                                    capture_output=True, timeout=10)
            return result, output.read_text(), summary.read_text()

    def test_only_fresh_bot_owned_reviews_pass_the_verdict(self):
        good = {"user": {"login": "github-actions[bot]"},
                "body": f"## PR Reviewer Guide\n\n{MARKER}\nReview",
                "updated_at": "2026-09-08T10:01:00Z"}
        for comments, success in (
            ([good], True), ([], False),
            ([{**good, "updated_at": "2026-09-08T09:59:00Z"}], False),
            ([{**good, "user": {"login": "someone"}}], False),
            ([{**good, "body": f"## Header\nquoted {MARKER}\n\n{MARKER}"}], False),
        ):
            with self.subTest(comments=comments):
                result, _, _ = self.run_step("Record review verdict", json.dumps(comments))
                self.assertEqual(result.returncode == 0, success, result.stderr)

    def test_invalid_api_response_fails_closed(self):
        for response in ("not json", "{}"):
            result, _, _ = self.run_step("Record review verdict", response)
            self.assertNotEqual(result.returncode, 0)

    def test_uv_lock_only_and_paginated_mixed_changes(self):
        for files, reviewable in ((["uv.lock"], False), (["nested/uv.lock", "poetry.lock"], False),
                                  (["uv.lock", "side_dog/cli.py"], True)):
            response = "\n".join(json.dumps([{"filename": name}]) for name in files)
            result, output, summary = self.run_step("Check for files PR-Agent will review", response)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"reviewable={str(reviewable).lower()}", output)
            if not reviewable:
                self.assertIn("no review is expected", summary)

    def test_fork_and_unknown_head_are_refused(self):
        for response, success in (("false", True), ("true", False), ("null", False), ("", False)):
            result, output, _ = self.run_step("Verify the pull request head is not a fork", response)
            self.assertEqual(result.returncode == 0, success)
            self.assertEqual("head_is_fork=false" in output, success)

    def test_review_command_uses_a_word_boundary(self):
        for command, review in (("/review", True), ("/review extra", True), ("/reviewer", False), ("/ask why", False)):
            result, output, _ = self.run_step("Classify the comment command", "", COMMENT_BODY=command)
            self.assertEqual(result.returncode, 0)
            self.assertIn(f"is_review={str(review).lower()}", output)

    def test_authentication_probe_reports_status_without_credential(self):
        for key, status, success in (("synthetic-key", "200", True),
                                     ("synthetic-key", "401", False),
                                     ("", "200", False), ("synthetic key", "200", False)):
            result, _, summary = self.run_step("Verify OpenAI authentication", "",
                                              OPENAI_API_KEY=key, FIXTURE_STATUS=status)
            self.assertEqual(result.returncode == 0, success)
            if key:
                self.assertNotIn(key, result.stdout + result.stderr + summary)
