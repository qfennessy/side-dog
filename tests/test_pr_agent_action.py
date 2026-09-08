"""Pin and configuration contracts for the credential-bearing local action."""
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PrAgentActionTest(unittest.TestCase):
    def test_action_pins_image_and_upstream_source(self):
        action = (ROOT / ".github/actions/pr-agent/action.yml").read_text()
        docker = (ROOT / ".github/actions/pr-agent/Dockerfile").read_text()
        self.assertRegex(action, r"using: docker\s+image: Dockerfile")
        self.assertRegex(docker, r"(?m)^FROM pragent/pr-agent:github_action@sha256:[a-f0-9]{64}$")
        self.assertRegex(docker, r"(?m)^ARG PR_AGENT_COMMIT=[a-f0-9]{40}$")
        self.assertIn("https://github.com/The-PR-Agent/pr-agent/archive/${PR_AGENT_COMMIT}.tar.gz", docker)
        self.assertIn('mv "/tmp/pr-agent-${PR_AGENT_COMMIT}/pr_agent" /app/pr_agent', docker)
        self.assertIn('python -c "import pr_agent.servers.github_action_runner"', docker)

    def test_one_bounded_reviewer_updates_a_persistent_comment(self):
        config = tomllib.loads((ROOT / ".pr_agent.toml").read_text())
        self.assertEqual(config["config"]["model"], "gpt-5.6-luna")
        self.assertTrue(config["config"]["output_run_details"])
        self.assertEqual(config["config"]["fallback_models"], [])
        self.assertNotIn("openrouter", config)
        self.assertEqual(config["config"]["reasoning_effort"], "high")
        self.assertEqual(config["config"]["ai_timeout"], 240)
        self.assertEqual(config["config"]["num_retries"], 0)
        self.assertFalse(config["config"]["retry_same_model_on_timeout"])
        self.assertNotIn(config["config"]["model"], config["config"]["fallback_models"])
        self.assertTrue(config["pr_reviewer"]["persistent_comment"])
        self.assertFalse(config["pr_reviewer"]["final_update_message"])
        self.assertEqual(config["github_action_config"], {
            "auto_describe": False, "auto_improve": False, "auto_review": True,
            "handle_push_trigger": True, "push_commands": ["/review"],
        })
        self.assertNotIn("best_practices", config)
