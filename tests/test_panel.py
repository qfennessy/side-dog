import http.client
import json
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import SCHEMA, build_parser
from side_dog.panel import (
    PANEL_HTML,
    PANEL_SCHEMA,
    PanelFeed,
    PanelServer,
    encode_sse,
    localhost_host,
    wire_unit,
)


class StubFeed:
    def snapshot(self) -> dict[str, object]:
        return {"schema": PANEL_SCHEMA, "type": "snapshot", "roots": [], "units": []}

    def poll(self) -> list[tuple[str, dict[str, object]]]:
        return []

    def close(self) -> None:
        return


class PanelTest(TestCase):
    def test_panel_parser_accepts_roots_and_safe_defaults(self) -> None:
        parser = build_parser()

        defaults = parser.parse_args(["panel"])
        configured = parser.parse_args(
            ["panel", "one", "two", "--port", "4321", "--poll", "1.5", "--no-open"]
        )

        self.assertEqual(defaults.projects, ["."])
        self.assertEqual(defaults.port, 0)
        self.assertFalse(defaults.no_open)
        self.assertEqual(configured.projects, ["one", "two"])
        self.assertEqual(configured.port, 4321)
        self.assertEqual(configured.poll, 1.5)
        self.assertTrue(configured.no_open)

    def test_wire_unit_has_stable_id_links_and_metadata_only(self) -> None:
        unit = {
            "root": "/tmp/project",
            "type": "event",
            "epoch": 1_000,
            "events": [
                {
                    "epoch_ms": 1_000,
                    "kind": "commit",
                    "title": "Commit",
                    "detail": "abcdef1 · add panel",
                    "git_oid": "abcdef1",
                    "status": "success",
                    "command": "secret command body",
                }
            ],
        }

        first = wire_unit(unit, "https://github.com/example/project")
        unit["events"][0]["repeat_count"] = 2
        second = wire_unit(unit, "https://github.com/example/project")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            first["url"], "https://github.com/example/project/commit/abcdef1"
        )
        self.assertNotIn("command", first["events"][0])
        self.assertNotIn("layout", first)

    def test_html_exposes_responsive_layout_and_timeline_controls(self) -> None:
        self.assertIn("Watching:", PANEL_HTML)
        self.assertIn('data-layout="columns"', PANEL_HTML)
        self.assertIn('data-layout="stack"', PANEL_HTML)
        self.assertIn("new EventSource('events')", PANEL_HTML)
        self.assertIn("ResizeObserver", PANEL_HTML)
        self.assertIn("e expand", PANEL_HTML)
        self.assertIn("f all", PANEL_HTML)
        self.assertIn("p pause", PANEL_HTML)
        self.assertIn("r oldest", PANEL_HTML)

    def test_sse_contract_is_versioned_and_named(self) -> None:
        payload = encode_sse("heartbeat", {"schema": PANEL_SCHEMA, "epoch_ms": 10})

        self.assertTrue(payload.startswith(b"event: heartbeat\n"))
        self.assertIn(b'"schema":"side-dog-panel-v1"', payload)
        self.assertTrue(payload.endswith(b"\n\n"))

    def test_only_loopback_host_headers_are_allowed(self) -> None:
        for value in ("localhost", "localhost:8000", "127.0.0.1:9000", "[::1]:80"):
            self.assertTrue(localhost_host(value), value)
        for value in (None, "example.com", "10.0.0.1", "127.example.com"):
            self.assertFalse(localhost_host(value), value)

    def test_server_requires_token_and_local_host_and_disables_caching(self) -> None:
        server = PanelServer(("127.0.0.1", 0), "private-token", StubFeed(), 0.1)  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/private-token/")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertIn(b"SIDE DOG", body)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.putrequest("GET", "/private-token/", skip_host=True)
            connection.putheader("Host", "example.com")
            connection.endheaders()
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 404)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/wrong-token/")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 404)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_feed_streams_model_units_and_refreshes_external_banners_async(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            event_log = Path(directory) / "events.jsonl"
            event_log.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "epoch_ms": 1_000,
                        "timestamp": "1970-01-01T00:00:01+00:00",
                        "kind": "file",
                        "title": "File changed",
                        "detail": "side_dog/panel.py",
                        "agent": "filesystem",
                        "status": "success",
                    }
                )
                + "\n"
            )
            identity = {
                "pane": {
                    "pane_id": "w1:p1",
                    "agent": "Codex",
                    "label": "side-dog",
                    "model": "gpt-test",
                    "effort": "high",
                    "status": "working",
                }
            }
            git = {"branch": "main", "oid": "abcdef123", "short_oid": "abcdef1"}
            with (
                patch("side_dog.panel.events_path", return_value=event_log),
                patch("side_dog.panel.load_git_state", return_value=git),
                patch("side_dog.panel.load_github_pr", return_value=(None, None)),
                patch("side_dog.panel.load_herdr_identities", return_value=identity),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root])
                try:
                    snapshot = feed.snapshot()
                    self.assertEqual(snapshot["schema"], PANEL_SCHEMA)
                    self.assertEqual(snapshot["roots"][0]["name"], "project")
                    self.assertEqual(snapshot["roots"][0]["git"]["branch"], "main")
                    self.assertEqual(len(snapshot["units"]), 1)

                    updates: list[tuple[str, dict[str, object]]] = []
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline and not any(
                        event == "banner" for event, _ in updates
                    ):
                        updates.extend(feed.poll())
                        time.sleep(0.01)
                    banners = [value for event, value in updates if event == "banner"]
                    self.assertEqual(banners[-1]["agents"][0]["model"], "gpt-test")
                finally:
                    feed.close()
