from pathlib import Path
from unittest import TestCase

from side_dog.cli import (
    actor_label,
    classify_commands,
    display_detail,
    display_title,
    github_event,
    render,
)


class RenderHelpTest(TestCase):
    def test_help_shows_controls_and_current_commit(self) -> None:
        screen = render(
            [],
            Path("/tmp/example-project"),
            width=80,
            height=24,
            color=False,
            identities={
                "codex-session": {
                    "agent": "codex",
                    "pane_id": "w1:p1",
                    "label": "Codex",
                    "model": "gpt-example",
                    "effort": "high",
                    "status": "working",
                }
            },
            git_status={
                "branch": "feature/sidebar",
                "oid": "1234567890abcdef",
                "short_oid": "1234567",
            },
            show_help=True,
        )

        self.assertIn("┌ Help", screen)
        self.assertIn("?       toggle this help", screen)
        self.assertIn("Git feature/sidebar · ◆ 1234567", screen)
        self.assertIn("Agent Codex · gpt-example · high · working", screen)

    def test_removed_file_label_is_compact(self) -> None:
        self.assertEqual(display_title({"title": "File removed"}), "removed")

    def test_verified_github_event_is_not_misattributed(self) -> None:
        event = github_event(
            {
                "number": 3,
                "title": "Add useful activity names",
                "state": "OPEN",
                "ci": "CI none",
            },
            None,
            {},
        )

        self.assertEqual(event["agent"], "github")
        self.assertEqual(actor_label(event, {}), "")
        self.assertIn("Add useful activity names", event["detail"])
        event["detail"] = "stale cached detail"
        self.assertIn("Add useful activity names", display_detail(event))

    def test_command_titles_are_helpful_without_recording_bodies(self) -> None:
        issue = classify_commands(
            "gh issue create --title 'Improve timeline layout' "
            "--body 'private implementation detail'"
        )
        pull_request = classify_commands(
            "gh pr create --title='Add activity names' --body-file notes.md"
        )

        self.assertEqual(issue, [("issue", "Opening issue", "Improve timeline layout")])
        self.assertEqual(
            pull_request,
            [("pr", "Opening pull request", "Add activity names")],
        )
        self.assertNotIn("private implementation detail", repr(issue))
