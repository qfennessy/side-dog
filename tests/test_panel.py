import http.client
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import Mock, patch

from side_dog.cli import SCHEMA, build_parser, folder_discovery_mode
from side_dog.panel import (
    ALLOWED_EVENT_FIELDS,
    BOARD_EVENT,
    BOARD_HTML,
    BOARD_LOGIC_JS,
    PANEL_HTML,
    PANEL_HIGHWAY_LOGIC_JS,
    PANEL_SCHEMA,
    BoardFeed,
    PanelFeed,
    PanelServer,
    board_wire,
    encode_sse,
    configured_filesystem_activity,
    localhost_host,
    wire_unit,
    panel,
)
from side_dog.privacy import SAFE_PANEL_WIRE_FIELDS
from side_dog.polling import PollBatch, PollCoordinator, PollStats, PollTarget


class StubFeed:
    show_filesystem_activity = False

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": PANEL_SCHEMA,
            "type": "snapshot",
            "display": {
                "show_filesystem_activity": self.show_filesystem_activity
            },
            "roots": [],
            "units": [],
        }

    def poll(self) -> list[tuple[str, dict[str, object]]]:
        return []

    def close(self) -> None:
        return

    def set_show_filesystem_activity(self, show: bool) -> bool:
        self.show_filesystem_activity = bool(show)
        return self.show_filesystem_activity


class RecordingPollCoordinator:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order
        self.ticks: list[tuple[PollTarget, ...]] = []
        self.close_wait: list[bool] = []

    def tick(self, targets: Any) -> tuple[object, ...]:
        if self.order is not None:
            self.order.append("coordinator")
        self.ticks.append(tuple(targets))
        return ()

    def close(self, *, wait: bool = True) -> None:
        self.close_wait.append(wait)


def _strings(value: object) -> list[str]:
    """Every string anywhere in a JSON-shaped value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            text
            for key, item in value.items()
            for text in (*_strings(key), *_strings(item))
        ]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _read_sse_event(response: http.client.HTTPResponse) -> tuple[str, dict[str, Any]]:
    """One ``event:``/``data:`` block from an open stream."""
    event = ""
    data = ""
    while True:
        line = response.readline().decode()
        if line == "":
            raise AssertionError("stream closed")
        if line == "\n":
            if event:
                return event, json.loads(data)
            continue
        name, _, value = line.rstrip("\n").partition(": ")
        if name == "event":
            event = value
        elif name == "data":
            data = value


def _board_message(**overrides: Any) -> dict[str, Any]:
    from side_dog.board import board_rows_payload

    message = board_wire(board_rows_payload([], []), settings={"group": "none", "detail": "shown"})
    message.update(overrides)
    return message


class BoardRouteTest(TestCase):
    """The panel's /board page, its JSON, and its stream."""

    def run_board_logic(self, source: str) -> Any:
        if shutil.which("node") is None:
            self.skipTest("Node is required for panel JavaScript behavior checks")
        completed = subprocess.run(
            ["node", "-e", BOARD_LOGIC_JS + "\n" + source],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)

    def start(self) -> tuple[PanelServer, threading.Thread]:
        server = PanelServer(("127.0.0.1", 0), "private-token", StubFeed(), 0.1)  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def stop(self, server: PanelServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    def test_the_pages_link_to_each_other(self) -> None:
        self.assertIn('href="board"', PANEL_HTML)
        self.assertIn('id="timeline"', BOARD_HTML)
        self.assertIn("base+'/board/events'", BOARD_HTML)
        self.assertIn("e.key==='g'", BOARD_HTML)
        self.assertIn("e.key==='d'", BOARD_HTML)
        self.assertIn("new URLSearchParams(location.search).get('group')", BOARD_HTML)
        # Every linked issue is its own anchor; the compact text is only the
        # fallback for a row without issues.
        self.assertIn("(row.issues||[]).map(issue=>link(issue.url,issue.label)).join(', ')", BOARD_HTML)
        # The shared header rule `.status{display:flex...}` must not turn the
        # STATUS cell into a flex box; the cell resets it to a table cell.
        self.assertIn(".status{display:flex;", BOARD_HTML)
        self.assertIn("td.status{display:table-cell;margin:0;", BOARD_HTML)
        self.assertIn("if(!issues.length)return esc(row.issue_text||'—')", BOARD_HTML)
        self.assertNotIn("link(first.url,row.issue_text)", BOARD_HTML)

    def test_the_board_is_polled_on_its_own_thread_only_while_wanted(self) -> None:
        class FakeBoardFeed:
            def __init__(self) -> None:
                self.polls = 0
                self.closed = False

            def poll(self) -> dict[str, Any] | None:
                self.polls += 1
                return _board_message(sessions=0, discovering=False, group="repo")

            def settings(self) -> dict[str, str]:
                return {"group": "repo", "detail": "hidden"}

            def close(self) -> None:
                self.closed = True

        class SlowFeed(StubFeed):
            polls = 0

            def poll(self) -> list[tuple[str, dict[str, object]]]:
                SlowFeed.polls += 1
                return []

        board_feed = FakeBoardFeed()
        server = PanelServer(("127.0.0.1", 0), "private-token", SlowFeed(), 0.02, board_feed=board_feed)  # type: ignore[arg-type]
        try:
            self.assertIsNotNone(server._board_thread)
            self.assertNotEqual(server._board_thread, server._feed_thread)
            time.sleep(0.15)
            # Nobody has asked for the roster: the timeline polls, the board does not.
            self.assertGreater(SlowFeed.polls, 0)
            self.assertEqual(board_feed.polls, 0)
            self.assertFalse(server.board_wanted())
            snapshot, updates = server.subscribe_board()
            self.assertTrue(snapshot["discovering"])
            # The placeholder already carries the configured defaults, so a
            # page that opens before the first walk does not start flat.
            self.assertEqual((snapshot["group"], snapshot["detail"]), ("repo", "hidden"))
            self.assertTrue(server.board_wanted())
            event, value = updates.get(timeout=1.0)
            self.assertEqual(event, BOARD_EVENT)
            self.assertFalse(value["discovering"])
            self.assertEqual(server.board_snapshot()["group"], "repo")
            server.unsubscribe_board(updates)
            # Interest lingers a while after the last stream closes, then lapses.
            self.assertTrue(server.board_wanted())
            self.assertFalse(server.board_wanted(time.monotonic() + 61.0))
        finally:
            server.server_close()
        self.assertTrue(board_feed.closed)

    def test_board_serves_html_and_json_behind_the_token(self) -> None:
        server, thread = self.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/private-token/board")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertIn("text/html", response.getheader("Content-Type") or "")
            self.assertIn(b"SIDE DOG", body)
            self.assertIn(b"No coding-agent sessions found", body)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/private-token/board/data")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["schema"], PANEL_SCHEMA)
            self.assertEqual(payload["type"], BOARD_EVENT)
            self.assertEqual(payload["rows"], [])
            self.assertEqual(payload["conflicts"], [])
            self.assertTrue(payload["discovering"])
            self.assertEqual((payload["group"], payload["detail"]), ("repo", "shown"))

            for path in ("/wrong-token/board", "/private-token/board/other"):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", path)
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 404, path)
        finally:
            self.stop(server, thread)

    def test_the_board_stream_sends_the_roster_then_only_board_updates(self) -> None:
        server, thread = self.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/private-token/board/events")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type") or "")
            event, first = _read_sse_event(response)
            self.assertEqual(event, BOARD_EVENT)
            self.assertEqual(first["rows"], [])

            # Timeline traffic stays on the timeline stream.
            server.publish("unit", {"id": "unit-1", "root": "/tmp/project", "events": []})
            update = _board_message(
                rows=[{"id": "abc", "agent_name": "Codex", "surface": "kitty", "status": "working"}],
                conflicts=[
                    "Possible coding-agent conflict — same folder for api: "
                    "kitty and Herdr · pane p3"
                ],
                sessions=1,
                discovering=False,
            )
            server.publish(BOARD_EVENT, update)
            event, second = _read_sse_event(response)
            self.assertEqual(event, BOARD_EVENT)
            self.assertEqual(second["rows"][0]["agent_name"], "Codex")
            self.assertEqual(
                second["conflicts"],
                [
                    "Possible coding-agent conflict — same folder for api: "
                    "kitty and Herdr · pane p3"
                ],
            )
            connection.close()

            # The JSON endpoint and a late subscriber see the same message.
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/private-token/board/data")
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read())["rows"], second["rows"])
            connection.close()
            snapshot, _ = server.subscribe()
            self.assertNotIn("rows", snapshot)
        finally:
            self.stop(server, thread)

    def test_the_feed_builds_rows_from_the_terminal_boards_sources_without_paths(self) -> None:
        from side_dog.cli import STATE_ENV
        from side_dog.integrations import _SAFE_GITHUB_FIELDS

        roots = [Path("/work/side-dog"), Path("/work/herdr")]
        identities: dict[str, dict[str, dict[str, str]]] = {
            "/work/side-dog": {
                "claude-code:c1": {
                    "agent": "claude-code",
                    "root": "/work/side-dog",
                    "working_root": "/work/side-dog",
                    "session_id": "c1",
                    "pane_id": "w1:p3",
                    "workspace_id": "side-dog",
                    "status": "working",
                    "label": "",
                    "surface": "",
                    "model": "claude-opus-4-1",
                },
                "codex:d1": {
                    "agent": "codex",
                    "root": "/work/side-dog",
                    "working_root": "/Users/q/.codex/worktrees/abc/side-dog",
                    "session_id": "d1",
                    "pane_id": "",
                    "status": "idle",
                    "label": "Codex Desktop · fix",
                    "surface": "Codex Desktop",
                },
            },
            "/work/herdr": {
                "claude-code:c2": {
                    "agent": "claude-code",
                    "root": "/work/herdr",
                    "working_root": "/work/herdr",
                    "session_id": "c2",
                    "pane_id": "",
                    "status": "working",
                    "label": "",
                    "surface": "Claude Desktop",
                },
                "codex:d2": {
                    "agent": "codex",
                    "root": "/work/herdr",
                    "working_root": "/work/herdr",
                    "session_id": "d2",
                    "pane_id": "",
                    "status": "working",
                    "label": "",
                    "surface": "Ghostty",
                },
            },
        }
        git_states = {
            "/work/side-dog": {"branch": "feat/board", "repository": "side-dog", "common_dir": "/work/side-dog/.git"},
            "/work/herdr": {"branch": "main", "repository": "herdr", "common_dir": "/work/herdr/.git"},
            "/Users/q/.codex/worktrees/abc/side-dog": {"branch": "codex/issue-139", "repository": "side-dog"},
        }
        github = {
            "number": 151,
            "url": "https://github.com/o/side-dog/pull/151",
            "title": "Add board",
            "state": "OPEN",
            "branch": "feat/board",
            "checks_total": 1,
            "checks_passed": 1,
            "checks_pending": 0,
            "checks_failed": 0,
            "closing_issues": (142,),
        }

        def fake_github(root: Path, branch: str | None = None):
            if root == roots[0]:
                return dict(github), None
            return None, "no pull requests found for branch"

        with TemporaryDirectory() as state_dir, patch.dict(
            os.environ, {STATE_ENV: state_dir}
        ), patch("side_dog.panel.discovered_watch_roots", return_value=roots), patch(
            "side_dog.panel.load_config", return_value={"board": {"group": "repo", "detail": "hidden"}}
        ), patch(
            "side_dog.cli.load_agent_identities",
            side_effect=lambda root: {key: dict(value) for key, value in identities[os.fspath(root)].items()},
        ), patch(
            "side_dog.cli.load_git_state",
            side_effect=lambda root: dict(git_states.get(os.fspath(root), {})) or None,
        ), patch("side_dog.cli.load_github_pr", side_effect=fake_github), patch(
            "side_dog.cli.canonical_root", side_effect=lambda value: Path(value)
        ), patch(
            "side_dog.cli.origin_repository",
            side_effect=lambda root: {"/work/herdr": "github.com/o/herdr"}.get(root, ""),
        ):
            feed = BoardFeed(github_poll=60.0)
            try:
                # The file's defaults are known before the first discovery.
                self.assertEqual(feed.settings(), {"group": "repo", "detail": "hidden"})
                first = feed.poll()
                self.assertIsNotNone(first)
                assert first is not None
                deadline = time.monotonic() + 2.0
                message = first
                while time.monotonic() < deadline:
                    later = feed.poll()
                    if later is not None:
                        message = later
                    if any(row["pr_text"].startswith("#151") for row in message["rows"]):
                        break
                    time.sleep(0.02)
                # Nothing changed: no message.
                self.assertIsNone(feed.poll())
                identities["/work/side-dog"]["codex:d1"]["status"] = "working"
                for state in feed._states.values():
                    state.last_identity_refresh = -1e9
                changed = feed.poll()
                self.assertIsNotNone(changed)
            finally:
                feed.close()

        self.assertEqual(message["schema"], PANEL_SCHEMA)
        self.assertEqual((message["group"], message["detail"]), ("repo", "hidden"))
        self.assertFalse(message["discovering"])
        self.assertEqual(message["sessions"], 4)
        self.assertEqual(message["repositories"], 2)
        by_agent = {(row["agent"], row["surface"]): row for row in message["rows"]}
        herdr = by_agent[("claude-code", "Herdr · side-dog · pane w1:p3")]
        self.assertEqual(herdr["repository"], "side-dog")
        self.assertEqual(herdr["branch"], "feat/board")
        self.assertEqual(herdr["pr_text"], "#151 ✓ci ○rev")
        self.assertEqual(herdr["pr_url"], "https://github.com/o/side-dog/pull/151")
        self.assertEqual(herdr["model"], "claude-opus-4-1")
        self.assertEqual(herdr["issues"][0]["label"], "#142")
        self.assertLessEqual(set(herdr["github"]), _SAFE_GITHUB_FIELDS)
        codex = by_agent[("codex", "Codex Desktop")]
        self.assertEqual(codex["branch"], "codex/issue-139")
        self.assertEqual(codex["issue_text"], "—")
        self.assertEqual(codex["status"], "idle")
        desktop = by_agent[("claude-code", "Claude Desktop")]
        self.assertEqual(desktop["repository_label"], "herdr")
        # Two coding agents share the herdr checkout: the strip names the
        # repository, not the folder, and the typed message validated on the way out.
        self.assertEqual(
            message["conflicts"],
            [
                "Possible coding-agent conflict — same folder "
                "for herdr: Claude Desktop and Ghostty"
            ],
        )
        for text in _strings(message):
            self.assertFalse(text.startswith("/"), text)
            for fragment in ("/Users/", "/home/", "/work/", ".git", "common_dir"):
                self.assertNotIn(fragment, text)
        for row in message["rows"]:
            self.assertLessEqual(
                set(row),
                {
                    "id", "agent", "agent_name", "surface", "repository", "repository_label",
                    "branch", "model", "status", "status_glyph", "age_seconds", "issue_text",
                    "issues", "pr_text", "pr_url", "github", "last_activity_ms",
                    "issues_omitted",
                },
            )
        self.assertEqual(changed["rows"][0]["status"] if changed else None, "working")

    def test_a_new_event_alone_is_pushed_while_the_clock_alone_is_not(self) -> None:
        from side_dog.cli import STATE_ENV, events_path
        from side_dog.privacy import safe_event

        root = Path("/work/side-dog")
        identities = {
            "claude-code:c1": {
                "agent": "claude-code",
                "root": "/work/side-dog",
                "working_root": "/work/side-dog",
                "session_id": "c1",
                "pane_id": "",
                "status": "working",
                "label": "",
                "surface": "kitty",
            }
        }

        def write_event(epoch_ms: int) -> None:
            history = events_path(root)
            history.parent.mkdir(parents=True, exist_ok=True)
            wire = safe_event(
                root,
                {
                    "agent": "claude-code",
                    "session_id": "c1",
                    "kind": "file",
                    "status": "success",
                    "title": "Wrote file",
                    "detail": "one file",
                },
            ).to_wire()
            # The boundary stamps "now"; the history is then backdated, the
            # way the once-command test does, so the age is deterministic.
            wire["epoch_ms"] = epoch_ms
            with history.open("a") as handle:
                handle.write(json.dumps(wire) + "\n")

        with TemporaryDirectory() as state_dir, patch.dict(
            os.environ, {STATE_ENV: state_dir}
        ), patch("side_dog.panel.discovered_watch_roots", return_value=[root]), patch(
            "side_dog.panel.load_config", return_value={}
        ), patch("side_dog.cli.load_agent_identities", return_value=identities), patch(
            "side_dog.cli.load_git_state", return_value={"branch": "main", "repository": "side-dog"}
        ), patch(
            "side_dog.cli.load_github_pr", return_value=(None, "no pull requests found for branch")
        ), patch("side_dog.cli.canonical_root", side_effect=lambda value: Path(value)), patch(
            "side_dog.cli.origin_repository", return_value=""
        ):
            now_ms = int(time.time() * 1000)
            write_event(now_ms - 60_000)
            feed = BoardFeed(github_poll=0.0)
            try:
                first = feed.poll()
                assert first is not None
                self.assertEqual(first["rows"][0]["last_activity_ms"], now_ms - 60_000)
                self.assertGreaterEqual(first["rows"][0]["age_seconds"], 59.0)
                # Time passes, nothing happens: the age grew but no event is sent.
                self.assertIsNone(feed.poll())
                # The session acts: the absolute time moves, so the age reset is sent.
                write_event(now_ms - 1_000)
                second = feed.poll()
                assert second is not None
                self.assertEqual(second["rows"][0]["last_activity_ms"], now_ms - 1_000)
                self.assertLess(second["rows"][0]["age_seconds"], 30.0)
                self.assertIsNone(feed.poll())
            finally:
                feed.close()

    def test_the_board_is_opt_in_and_the_panel_command_asks_for_it(self) -> None:
        from side_dog.panel import create_panel_server

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch("side_dog.panel._github_web_root", return_value=""):
                server, url = create_panel_server([root], poll_seconds=0.1)
            try:
                self.assertIsNone(server.board_feed)
                self.assertIsNone(server._board_thread)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                token = url.rstrip("/").rsplit("/", 1)[-1]
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", f"/{token}/board/data")
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(payload["rows"], [])
                self.assertEqual(payload["sessions"], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)
            with patch("side_dog.panel._github_web_root", return_value=""):
                server, _ = create_panel_server([root], poll_seconds=0.1, board=True)
            try:
                self.assertIsInstance(server.board_feed, BoardFeed)
            finally:
                server.server_close()

        calls: list[dict[str, Any]] = []

        def fake_create(roots: Any, **kwargs: Any) -> tuple[Any, str]:
            calls.append(kwargs)
            fake = Mock()
            fake.serve_forever.side_effect = KeyboardInterrupt
            return fake, "http://127.0.0.1/example/"

        with patch("side_dog.panel.create_panel_server", side_effect=fake_create), patch(
            "side_dog.panel.initial_watch_roots", return_value=([], set(), None)
        ):
            panel([], open_window=False)
            panel([], open_window=False, board=False)
        self.assertEqual([call["board"] for call in calls], [True, False])
        parser = build_parser()
        self.assertTrue(parser.parse_args(["panel", "--no-board"]).no_board)
        self.assertFalse(parser.parse_args(["panel"]).no_board)

    def test_the_board_page_logic_groups_and_ages_rows(self) -> None:
        result = self.run_board_logic(
            """
const rows=[
 {id:'a',surface:'unknown',repository:'side-dog',repository_label:'side-dog',age_seconds:5},
 {id:'b',surface:'Herdr · pane p3',repository:'',repository_label:'',age_seconds:null},
 {id:'c',surface:'Codex Desktop',repository:'api',repository_label:'api (o)',age_seconds:3700},
 {id:'d',surface:'Codex Desktop',repository:'side-dog',repository_label:'side-dog',age_seconds:61}];
console.log(JSON.stringify({
 query:boardGroup('repo','surface'),configured:boardGroup('bogus','surface'),fallback:boardGroup('','nope'),
 next:[nextBoardGroup('none'),nextBoardGroup('repo')],
 surface:boardSections(rows,'surface').map(s=>[s.label,s.rows.map(r=>r.id)]),
 repo:boardSections(rows,'repo').map(s=>[s.label,s.rows.map(r=>r.id)]),
 flat:boardSections(rows,'none').map(s=>[s.label,s.rows.map(r=>r.id)]),
 ages:[formatAge(5),formatAge(61),formatAge(3700),formatAge(90000),formatAge(null)],
 live:liveAge(rows[0],1000,31000),still:liveAge(rows[1],1000,31000),
 summary:boardSummary({sessions:1,repositories:0,discovering:true}),
 repo_cell:[repoCell(rows[3],'repo'),repoCell({repository:'api',branch:'main'},'none')],
 url:[webUrl('https://github.com/o/r/pull/1'),webUrl('javascript:alert(1)'),webUrl('')],
 overflow:[issueOverflow({issues:[{number:1}],issues_omitted:41}),issueOverflow({issues:[{number:1}],issues_omitted:0}),issueOverflow({issues:[]})],
 resolve:[
  resolveGroup(null,'',{discovering:true,group:'none'}),
  resolveGroup(null,'',{discovering:false,group:'repo'}),
  resolveGroup(null,'surface',{discovering:true,group:'none'}),
  resolveGroup('none','',{discovering:false,group:'repo'}),
  resolveGroup(null,'bogus',{discovering:false,group:'surface'})]
}));
"""
        )
        # While the placeholder is still discovering, the page keeps its
        # choice open unless ?group= says otherwise; the first real message
        # applies the configured group; a person's choice is never overridden.
        self.assertEqual(result["resolve"], [None, "repo", "surface", "none", "surface"])
        # A bounded issue list says how many links it left out, in both the
        # cell and the detail row, which share issueLinks().
        self.assertEqual(result["overflow"], [" +41", "", ""])
        self.assertIn("function issueLinks(row)", BOARD_HTML)
        self.assertIn("esc(issueOverflow(row))", BOARD_HTML)
        self.assertIn("parts.push(issueLinks(row))", BOARD_HTML)
        self.assertEqual(result["query"], "repo")
        self.assertEqual(result["configured"], "surface")
        self.assertEqual(result["fallback"], "repo")
        self.assertEqual(result["next"], ["surface", "none"])
        self.assertEqual(
            result["surface"],
            [["Codex Desktop", ["c", "d"]], ["Herdr · pane p3", ["b"]], ["unknown", ["a"]]],
        )
        self.assertEqual(
            result["repo"],
            [["api (o)", ["c"]], ["side-dog", ["a", "d"]], ["no repository", ["b"]]],
        )
        self.assertEqual(result["flat"], [["", ["a", "b", "c", "d"]]])
        self.assertEqual(result["ages"], ["5s", "1m", "1h", "1d", ""])
        self.assertEqual(result["live"], 35)
        self.assertIsNone(result["still"])
        self.assertEqual(result["summary"], "1 session · discovering")
        self.assertEqual(result["repo_cell"], ["", "api  main"])
        self.assertEqual(result["url"], ["https://github.com/o/r/pull/1", "", ""])


class PanelTest(TestCase):
    def run_highway_logic(self, source: str) -> Any:
        if shutil.which("node") is None:
            self.skipTest("Node is required for panel JavaScript behavior checks")
        completed = subprocess.run(
            ["node", "-e", PANEL_HIGHWAY_LOGIC_JS + "\n" + source],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)

    def test_feed_polls_all_roots_with_one_coordinator_tick_first(self) -> None:
        with TemporaryDirectory() as directory:
            first = (Path(directory) / "first").resolve()
            second = (Path(directory) / "second").resolve()
            first.mkdir()
            second.mkdir()
            order: list[str] = []
            coordinator = RecordingPollCoordinator(order)

            def read_events(*_args: object) -> tuple[list[object], int]:
                order.append("events")
                return [], 0

            with (
                patch(
                    "side_dog.panel.events_path",
                    side_effect=lambda root: root / "events.jsonl",
                ),
                patch("side_dog.panel.read_new_events", side_effect=read_events),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed(
                    [first, second],
                    follow_worktrees=False,
                    poll_coordinator=coordinator,  # type: ignore[arg-type]
                )
                try:
                    feed.roots[0].identities = {
                        "codex:one": {
                            "agent": "codex",
                            "session_id": "one",
                            "root": str(first),
                        }
                    }
                    order.clear()

                    feed.poll()

                    self.assertEqual(order[0], "coordinator")
                    self.assertEqual(len(coordinator.ticks), 1)
                    targets = coordinator.ticks[0]
                    self.assertEqual(
                        [target.root for target in targets],
                        [first, second],
                    )
                    self.assertEqual(
                        targets[0].for_provider("codex")[0].session_id,
                        "one",
                    )
                finally:
                    feed.close()

            self.assertEqual(coordinator.close_wait, [False])

    def test_poll_notifies_for_a_new_test_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            failure = {
                "kind": "test",
                "status": "failed",
                "title": "Tests failed",
                "detail": "pytest",
            }
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch(
                    "side_dog.panel.read_new_events",
                    side_effect=[([], 0), ([failure], 1)],
                ),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch("side_dog.panel.notify_for_event") as notified,
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    feed.poll()
                    notified.assert_called_once_with(feed.roots[0].label, failure)
                finally:
                    feed.close()

    def test_notify_false_sends_no_desktop_notification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            failure = {
                "kind": "test",
                "status": "failed",
                "title": "Tests failed",
                "detail": "pytest",
            }
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch(
                    "side_dog.panel.read_new_events",
                    side_effect=[([], 0), ([failure], 1)],
                ),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch("side_dog.panel.notify_for_event") as notified,
            ):
                feed = PanelFeed([root], follow_worktrees=False, notify=False)
                try:
                    feed.poll()
                    notified.assert_not_called()
                finally:
                    feed.close()

    def test_slow_adapter_does_not_block_poll_or_close(self) -> None:
        class SlowAdapter:
            provider = "codex"

            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()
                self.closed = threading.Event()

            def poll(self, _targets: object) -> PollBatch:
                self.started.set()
                self.release.wait(5)
                return PollBatch(PollStats(self.provider))

            def close(self) -> None:
                self.closed.set()

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            adapter = SlowAdapter()
            coordinator = PollCoordinator([adapter])
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed(
                    [root],
                    follow_worktrees=False,
                    poll_coordinator=coordinator,
                )
                started = time.monotonic()
                feed.poll()
                poll_elapsed = time.monotonic() - started
                self.assertTrue(adapter.started.wait(0.5))

                started = time.monotonic()
                feed.close()
                close_elapsed = time.monotonic() - started

            self.assertLess(poll_elapsed, 0.5)
            self.assertLess(close_elapsed, 0.5)
            self.assertFalse(adapter.closed.is_set())
            adapter.release.set()
            self.assertTrue(adapter.closed.wait(0.5))

    def test_server_broadcasts_the_same_delta_to_every_subscriber(self) -> None:
        server = PanelServer(
            ("127.0.0.1", 0),
            "private-token",
            StubFeed(),
            0.1,  # type: ignore[arg-type]
        )
        try:
            _, first = server.subscribe()
            _, second = server.subscribe()
            unit = {"id": "unit-1", "root": "/tmp/project", "events": []}

            server.publish("unit", unit)

            self.assertEqual(first.get(timeout=0.1), ("unit", unit))
            self.assertEqual(second.get(timeout=0.1), ("unit", unit))
        finally:
            server.server_close()

    def test_server_bind_failure_closes_feed_and_preserves_oserror(self) -> None:
        class RecordingFeed(StubFeed):
            def __init__(self) -> None:
                self.close_count = 0

            def close(self) -> None:
                self.close_count += 1

        feed = RecordingFeed()
        bind_error = OSError("address unavailable")

        with (
            patch(
                "side_dog.panel.ThreadingHTTPServer.server_bind",
                side_effect=bind_error,
            ),
            self.assertRaises(OSError) as raised,
        ):
            PanelServer(
                ("127.0.0.1", 0),
                "private-token",
                feed,  # type: ignore[arg-type]
                0.1,
            )

        self.assertIs(raised.exception, bind_error)
        self.assertEqual(feed.close_count, 1)

    def test_agent_rows_are_scoped_to_the_exact_worktree(self) -> None:
        root = Path("/tmp/repo-main")
        identities = {
            "main": {
                "pane_id": "main",
                "root": "/tmp/repo-main",
                "working_root": "/tmp/repo-main/src",
                "agent": "Codex",
                "label": "main agent",
            },
            "sibling": {
                "pane_id": "sibling",
                "root": "/tmp/repo-feature",
                "working_root": "/tmp/repo-feature/src",
                "agent": "Codex",
                "label": "feature agent",
            },
        }

        rows = PanelFeed._agent_rows(identities, root)

        self.assertEqual([row["label"] for row in rows], ["main agent"])

    def test_agent_rows_separate_codex_desktop_surface_from_task(self) -> None:
        rows = PanelFeed._agent_rows(
            {
                "desktop": {
                    "pane_id": "desktop",
                    "root": "/tmp/repo-main",
                    "working_root": "/tmp/repo-main",
                    "agent": "codex",
                    "label": "Codex Desktop · header polish",
                }
            },
            Path("/tmp/repo-main"),
        )

        self.assertEqual(rows[0]["task"], "header polish")
        self.assertEqual(rows[0]["surface"], "Codex Desktop")

    def test_feed_adopts_agents_that_join_the_herdr_session_later(self) -> None:
        with TemporaryDirectory() as directory:
            first = (Path(directory) / "first").resolve()
            second = (Path(directory) / "second").resolve()
            first.mkdir()
            second.mkdir()
            with (
                patch(
                    "side_dog.panel.events_path",
                    side_effect=lambda root: root / "events.jsonl",
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch(
                    "side_dog.panel.herdr_session_roots",
                    return_value=([first, second], None),
                ),
            ):
                feed = PanelFeed(
                    [first],
                    follow_worktrees=False,
                    follow_herdr=True,
                    requested_roots=[],
                )
                try:
                    original_mode = feed.discovery_mode
                    self.assertTrue(feed._follow_worktree_changes(10.0))
                    self.assertEqual(
                        [state.root for state in feed.roots], [first, second]
                    )
                    self.assertEqual(feed.discovery_mode, original_mode)
                    self.assertEqual(feed.discovery_mode.key, "herdr-agents")
                finally:
                    feed.close()

    def test_feed_keeps_workspace_scope_during_herdr_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "root").resolve()
            outside = (Path(directory) / "outside").resolve()
            root.mkdir()
            outside.mkdir()
            with (
                patch(
                    "side_dog.panel.events_path",
                    side_effect=lambda value: value / "events.jsonl",
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch(
                    "side_dog.panel.herdr_session_roots",
                    return_value=([root], None),
                ) as herdr_roots,
                patch("side_dog.panel.busy_worktrees", return_value=[outside]),
            ):
                feed = PanelFeed(
                    [root],
                    follow_worktrees=True,
                    follow_herdr=True,
                    workspace_id="wN",
                    requested_roots=[],
                )
                try:
                    feed._follow_worktree_changes(10.0)
                    self.assertEqual([state.root for state in feed.roots], [root])
                finally:
                    feed.close()

            herdr_roots.assert_called_once_with("wN")

    def test_herdr_reconciliation_does_not_evict_a_configured_pin(self) -> None:
        with TemporaryDirectory() as directory:
            pinned = (Path(directory) / "pinned").resolve()
            stale = (Path(directory) / "stale").resolve()
            live = (Path(directory) / "live").resolve()
            for root in (pinned, stale, live):
                root.mkdir()
            with (
                patch(
                    "side_dog.panel.events_path",
                    side_effect=lambda value: value / "events.jsonl",
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch("side_dog.panel.pinned_folders", return_value=[pinned]),
                patch("side_dog.panel.watch_root_limit", return_value=2),
                patch(
                    "side_dog.panel.herdr_session_roots",
                    return_value=([live], None),
                ),
                patch("side_dog.panel.busy_worktrees", return_value=[]),
                patch(
                    "side_dog.panel.folder_is_finished",
                    side_effect=lambda root: root == pinned,
                ),
            ):
                feed = PanelFeed(
                    [pinned, stale],
                    follow_worktrees=False,
                    follow_herdr=True,
                    requested_roots=[],
                )
                try:
                    self.assertTrue(feed._follow_worktree_changes(10.0))
                    self.assertEqual(
                        [state.root for state in feed.roots], [pinned, live]
                    )
                finally:
                    feed.close()

    def test_automatic_feed_discovers_a_repository_that_started_later(self) -> None:
        with TemporaryDirectory() as directory:
            pinned = (Path(directory) / "pinned").resolve()
            idle = (Path(directory) / "idle").resolve()
            newcomer = (Path(directory) / "newcomer").resolve()
            for root in (pinned, idle, newcomer):
                root.mkdir()
            configuration = {
                "pin": [str(pinned)],
                "display": {"limit": 2},
            }
            mode = folder_discovery_mode(
                explicit_roots=False, follow_herdr=False, require_herdr=False
            )
            with (
                patch(
                    "side_dog.panel.events_path",
                    side_effect=lambda root: root / "events.jsonl",
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch("side_dog.panel.load_config", return_value=configuration),
                patch("side_dog.panel.pinned_folders", return_value=[pinned]),
                patch("side_dog.panel.watch_root_limit", return_value=2),
                patch(
                    "side_dog.cli.agent_working_folders",
                    return_value={newcomer: True},
                ),
                patch("side_dog.panel.busy_worktrees", return_value=[]),
                patch("side_dog.panel.folder_is_finished", return_value=False),
            ):
                feed = PanelFeed(
                    [pinned, idle], requested_roots=[], discovery_mode=mode
                )
                try:
                    self.assertTrue(feed._follow_worktree_changes(10.0))
                    self.assertEqual(
                        [state.root for state in feed.roots], [pinned, newcomer]
                    )
                finally:
                    feed.close()

    def test_named_folder_feed_does_not_run_machine_wide_discovery(self) -> None:
        root = Path("/tmp/named")
        mode = folder_discovery_mode(
            explicit_roots=True, follow_herdr=False, require_herdr=False
        )
        with (
            patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
            patch("side_dog.panel._github_web_root", return_value=""),
            patch("side_dog.panel.pinned_folders", return_value=[]),
            patch("side_dog.panel.rediscovered_roots") as rediscover,
            patch("side_dog.panel.busy_worktrees", return_value=[]),
        ):
            feed = PanelFeed([root], discovery_mode=mode)
            try:
                self.assertFalse(feed._follow_worktree_changes(10.0))
                rediscover.assert_not_called()
            finally:
                feed.close()

    def test_automatic_feed_does_not_retire_an_active_finished_root(self) -> None:
        active = Path("/tmp/active")
        other = Path("/tmp/other")
        mode = folder_discovery_mode(
            explicit_roots=False, follow_herdr=False, require_herdr=False
        )
        with (
            patch(
                "side_dog.panel.events_path",
                side_effect=lambda root: root / "events.jsonl",
            ),
            patch("side_dog.panel._github_web_root", return_value=""),
            patch("side_dog.panel.load_config", return_value={}),
            patch("side_dog.panel.pinned_folders", return_value=[]),
            patch("side_dog.panel.rediscovered_roots", return_value=([], [])),
            patch(
                "side_dog.panel.agent_working_folders",
                return_value={active: True},
            ),
            patch("side_dog.panel.busy_worktrees", return_value=[]),
            patch(
                "side_dog.panel.folder_is_finished",
                side_effect=lambda root: root == active,
            ),
        ):
            feed = PanelFeed(
                [active, other], requested_roots=[], discovery_mode=mode
            )
            try:
                self.assertFalse(feed._follow_worktree_changes(10.0))
                self.assertEqual(
                    [state.root for state in feed.roots], [active, other]
                )
            finally:
                feed.close()

    def test_feed_sends_snapshot_when_a_herdr_root_is_retired(self) -> None:
        with TemporaryDirectory() as directory:
            retired = (Path(directory) / "retired").resolve()
            live = (Path(directory) / "live").resolve()
            retired.mkdir()
            live.mkdir()
            with (
                patch(
                    "side_dog.panel.events_path",
                    side_effect=lambda root: root / "events.jsonl",
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
                patch(
                    "side_dog.panel.herdr_session_roots",
                    return_value=([live], None),
                ),
                patch(
                    "side_dog.panel.reconcile_herdr_roots",
                    return_value=([retired], [live]),
                ),
            ):
                feed = PanelFeed(
                    [retired],
                    follow_worktrees=False,
                    follow_herdr=True,
                    requested_roots=[],
                )
                try:
                    feed.snapshot()
                    updates = feed.poll()
                    snapshots = [
                        value for event, value in updates if event == "snapshot"
                    ]

                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(
                        [root["id"] for root in snapshots[0]["roots"]],
                        [str(live)],
                    )
                finally:
                    feed.close()

    def test_panel_parser_accepts_roots_and_safe_defaults(self) -> None:
        parser = build_parser()

        defaults = parser.parse_args(["panel"])
        configured = parser.parse_args(
            ["panel", "one", "two", "--port", "4321", "--poll", "1.5", "--no-open"]
        )

        self.assertEqual(defaults.projects, [])
        self.assertEqual(defaults.port, 0)
        self.assertFalse(defaults.no_open)
        self.assertFalse(defaults.herdr)
        self.assertTrue(parser.parse_args(["panel", "--herdr"]).herdr)
        self.assertEqual(configured.projects, ["one", "two"])
        self.assertEqual(configured.port, 4321)
        self.assertEqual(configured.poll, 1.5)
        self.assertTrue(configured.no_open)

    def test_bare_panel_outside_herdr_uses_current_folder_mode(self) -> None:
        with (
            patch("side_dog.panel.initial_watch_roots", return_value=([Path.cwd()], set(), None)),
            patch("side_dog.panel.create_panel_server") as create,
        ):
            server = create.return_value[0]
            create.return_value = (server, "http://127.0.0.1/example/")
            server.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(panel([], open_window=False), 0)

        mode = create.call_args.kwargs["discovery_mode"]
        self.assertEqual(mode.key, "current-folder")

    def test_automatic_panel_treats_its_initial_roots_as_discovered(self) -> None:
        root = Path("/tmp/discovered")
        with (
            patch(
                "side_dog.panel.initial_watch_roots",
                return_value=([root], {root}, None),
            ),
            patch("side_dog.panel.create_panel_server") as create,
        ):
            server = create.return_value[0]
            create.return_value = (server, "http://127.0.0.1/example/")
            server.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(
                panel(
                    [str(root)],
                    open_window=False,
                    discovery_mode_key="automatic",
                ),
                0,
            )

        self.assertEqual(create.call_args.kwargs["requested_roots"], set())

    def test_panel_preserves_a_scalar_project_path(self) -> None:
        project = "/tmp/project"
        with (
            patch(
                "side_dog.panel.initial_watch_roots",
                return_value=([Path(project)], {Path(project)}, None),
            ) as initial_roots,
            patch("side_dog.panel.create_panel_server") as create,
        ):
            server = create.return_value[0]
            create.return_value = (server, "http://127.0.0.1/example/")
            server.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(panel(project, open_window=False), 0)

        initial_roots.assert_called_once_with(
            [project],
            follow_herdr=False,
            require_herdr=False,
            workspace_id=None,
        )
        self.assertEqual(create.call_args.kwargs["discovery_mode"].key, "explicit")

    def test_wire_unit_has_stable_id_links_and_metadata_only(self) -> None:
        self.assertEqual(
            ALLOWED_EVENT_FIELDS,
            SAFE_PANEL_WIRE_FIELDS | {"first_timestamp", "repeat_count"},
        )
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
        unit["events"][0]["first_timestamp"] = "2026-09-02T10:14:00+00:00"
        unit["events"][0]["repeat_count"] = 2
        second = wire_unit(unit, "https://github.com/example/project")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            first["url"], "https://github.com/example/project/commit/abcdef1"
        )
        self.assertNotIn("command", first["events"][0])
        self.assertNotIn("layout", first)
        self.assertEqual(second["events"][0]["repeat_count"], 2)
        self.assertEqual(
            second["events"][0]["first_timestamp"],
            "2026-09-02T10:14:00+00:00",
        )

    def test_web_timeline_shows_repeated_context_count_and_time_span(self) -> None:
        result = self.run_highway_logic(
            """
const unit={epoch:Date.parse('2026-09-02T10:15:00Z')};
const event={title:'Read file',detail:'panel.py',repeat_count:5,first_timestamp:'2026-09-02T10:14:00Z'};
console.log(JSON.stringify({when:eventWhen(unit,event),text:eventText(event)}));
"""
        )

        self.assertIn("→", result["when"])
        self.assertEqual(result["text"], "Read file · panel.py · ×5")

    def test_wire_unit_removes_non_http_event_urls(self) -> None:
        unit = {
            "root": "/tmp/project",
            "type": "event",
            "epoch": 1_000,
            "events": [
                {
                    "epoch_ms": 1_000,
                    "kind": "github",
                    "status": "success",
                    "title": "Changed pull request",
                    "url": "javascript:alert('private')",
                    "github": {"url": "javascript:alert('nested-private')"},
                }
            ],
        }

        rendered = wire_unit(unit, "")

        self.assertNotIn("url", rendered)
        self.assertNotIn("url", rendered["events"][0])
        self.assertNotIn("javascript:", json.dumps(rendered))

    def test_html_exposes_responsive_layout_and_timeline_controls(self) -> None:
        self.assertIn("Watching:", PANEL_HTML)
        self.assertIn('data-layout="columns"', PANEL_HTML)
        self.assertIn('data-layout="stack"', PANEL_HTML)
        self.assertIn("new EventSource('events')", PANEL_HTML)
        self.assertIn("window.addEventListener('resize',renderResponsiveChrome)", PANEL_HTML)
        self.assertIn("e expand", PANEL_HTML)
        self.assertIn("API est $", PANEL_HTML)
        self.assertIn(
            "API estimate = public list prices applied to local logs; "
            "not a subscription bill",
            PANEL_HTML,
        )
        self.assertIn("f all", PANEL_HTML)
        self.assertIn("p pause", PANEL_HTML)
        self.assertIn("r oldest", PANEL_HTML)
        self.assertIn("h highway", PANEL_HTML)
        self.assertIn("s 1×", PANEL_HTML)
        self.assertIn(
            '<button id="filesystem" title="Toggle background activity">F show background</button>',
            PANEL_HTML,
        )
        self.assertIn("function toggleFilesystemActivity()", PANEL_HTML)
        self.assertIn("fetch('display'", PANEL_HTML)

    def test_panel_display_default_prefers_remembered_value_over_toml(self) -> None:
        with (
            patch(
                "side_dog.panel.load_display_settings",
                return_value={"show_filesystem_activity": False},
            ),
            patch(
                "side_dog.panel.load_config",
                return_value={"display": {"show_filesystem_activity": True}},
            ),
        ):
            self.assertFalse(configured_filesystem_activity())

        with (
            patch("side_dog.panel.load_display_settings", return_value={}),
            patch(
                "side_dog.panel.load_config",
                return_value={"display": {"show_filesystem_activity": True}},
            ),
        ):
            self.assertTrue(configured_filesystem_activity())

        with (
            patch(
                "side_dog.panel.load_display_settings",
                return_value={"show_filesystem_activity": "yes"},
            ),
            patch(
                "side_dog.panel.load_config",
                return_value={"display": {"show_filesystem_activity": True}},
            ),
        ):
            self.assertFalse(configured_filesystem_activity())

    def test_idle_agents_are_hidden_by_default(self) -> None:
        result = self.run_highway_logic(
            """
const agents=[
 {agent:'Codex',status:'working',label:'a'},
 {agent:'Codex',status:'idle',label:'b'},
 {agent:'Codex',status:'unknown',label:'c'},
 {agent:'Codex',status:'',label:'d'}
];
console.log(JSON.stringify({
 visible:visibleAgents(agents,false).map(a=>a.label),
 all:visibleAgents(agents,true).map(a=>a.label),
 hidden:hiddenIdleCount(agents,false),
 shown:hiddenIdleCount(agents,true)
}));
"""
        )

        self.assertEqual(result["visible"], ["a", "c", "d"])
        self.assertEqual(result["all"], ["a", "b", "c", "d"])
        self.assertEqual(result["hidden"], 1)
        self.assertEqual(result["shown"], 0)

    def test_semantic_statuses_have_text_and_glyphs_without_color(self) -> None:
        result = self.run_highway_logic(
            """
console.log(JSON.stringify({
 success:semanticStatus('success'),
 running:semanticStatus('working'),
 operationRunning:semanticStatus('running'),
 warning:semanticStatus('partial'),
 failed:semanticStatus('blocked'),
 idle:semanticStatus('idle'),
 unknown:semanticStatus('mystery')
}));
"""
        )

        expected = {
            "success": {"role": "success", "glyph": "✓", "label": "completed"},
            "running": {"role": "running", "glyph": "…", "label": "working"},
            "operationRunning": {
                "role": "running",
                "glyph": "…",
                "label": "running",
            },
            "warning": {"role": "warning", "glyph": "!", "label": "warning"},
            "failed": {"role": "failed", "glyph": "×", "label": "blocked"},
            "idle": {"role": "idle", "glyph": "○", "label": "idle"},
            "unknown": {"role": "unknown", "glyph": "·", "label": "unknown"},
        }
        self.assertEqual(result, expected)

    def test_panel_uses_documented_semantic_roles_in_light_and_dark_modes(self) -> None:
        for role in (
            "--navigation:",
            "--selection:",
            "--identity:",
            "--success:",
            "--attention:",
            "--failure:",
            "--idle:",
            "--unknown:",
        ):
            self.assertIn(role, PANEL_HTML)
        self.assertIn("@media(prefers-color-scheme:light)", PANEL_HTML)
        self.assertIn("statusMarker(e.status)", PANEL_HTML)
        self.assertIn("agentStatusHTML(a.status)", PANEL_HTML)
        self.assertNotIn("const klass=", PANEL_HTML)

    def test_idle_button_label_reports_hidden_count_and_toggles(self) -> None:
        result = self.run_highway_logic(
            """
console.log(JSON.stringify({
 hidden:idleButtonLabel(false,3),
 hiddenNone:idleButtonLabel(false,0),
 shown:idleButtonLabel(true,3)
}));
"""
        )

        self.assertEqual(result["hidden"], "i show idle (3)")
        self.assertEqual(result["hiddenNone"], "i show idle")
        self.assertEqual(result["shown"], "i hide idle")

    def test_an_idle_agent_reappears_when_it_starts_working(self) -> None:
        result = self.run_highway_logic(
            """
const idle=visibleAgents([{agent:'Codex',status:'idle',label:'x'}],false);
const working=visibleAgents([{agent:'Codex',status:'working',label:'x'}],false);
console.log(JSON.stringify({idle:idle.length,working:working.length}));
"""
        )

        self.assertEqual(result, {"idle": 0, "working": 1})

    def test_html_exposes_idle_agent_control(self) -> None:
        self.assertIn('<button id="idle">i show idle</button>', PANEL_HTML)
        self.assertIn("e.key==='i')toggleIdle()", PANEL_HTML)
        self.assertIn("function toggleIdle()", PANEL_HTML)
        self.assertIn("showIdle:false", PANEL_HTML)
        self.assertIn("visibleAgents(allAgents,state.showIdle)", PANEL_HTML)
        self.assertIn("idleButtonLabel(state.showIdle,idleTotal)", PANEL_HTML)

    def test_html_exposes_the_filesystem_visibility_control(self) -> None:
        self.assertIn("showFilesystemActivity:false", PANEL_HTML)
        self.assertIn("isPassiveFilesystemEvent", PANEL_HTML)
        self.assertIn("isLifecycleEvent", PANEL_HTML)
        self.assertIn("Background activity", PANEL_HTML)
        self.assertIn("panelAgentHTML", PANEL_HTML)
        self.assertIn(
            "message.display?.show_filesystem_activity===true", PANEL_HTML
        )
        self.assertNotIn("displayInitialized", PANEL_HTML)
        self.assertIn("e.key==='F')toggleFilesystemActivity()", PANEL_HTML)
        self.assertIn("visibleUnits", PANEL_HTML)

    def test_lifecycle_rows_follow_the_background_visibility_toggle(self) -> None:
        result = self.run_highway_logic(
            """
const lifecycle={kind:'lifecycle',status:'success',title:'Claude turn finished'};
console.log(JSON.stringify({hidden:backgroundEventAllowed(lifecycle,false),shown:backgroundEventAllowed(lifecycle,true)}));
"""
        )

        self.assertEqual(result, {"hidden": False, "shown": True})

    def test_highway_resolves_operations_and_never_crosses_roots(self) -> None:
        snapshot = self.run_highway_logic(
            """
const units=[
 {id:'running',root:'root-a',events:[{kind:'test',status:'running',operation_id:'op-1',epoch_ms:1000,started_epoch_ms:1000,title:'Running tests'}]},
 {id:'complete',root:'root-a',events:[{kind:'test',status:'success',operation_id:'op-1',epoch_ms:2000,started_epoch_ms:1000,title:'Tests passed'}]},
 {id:'other-root',root:'root-b',events:[{kind:'test',status:'failed',operation_id:'op-1',epoch_ms:2500,title:'Tests failed'}]}
];
console.log(JSON.stringify(highwaySnapshot(units,'root-a',3000,1)));
"""
        )

        self.assertEqual(snapshot["combo"], 1)
        self.assertEqual(len(snapshot["marks"]), 1)
        mark = snapshot["marks"][0]
        self.assertEqual(mark["root"], "root-a")
        self.assertEqual(mark["lane"], "tests")
        self.assertEqual(mark["status"], "success")
        self.assertEqual(mark["judgment"], "PASS")
        self.assertEqual(mark["hold"], 0)

    def test_highway_keeps_each_delivery_stage_in_its_lane(self) -> None:
        snapshot = self.run_highway_logic(
            """
const units=[{id:'delivery',root:'root-a',type:'pipeline',url:'https://example.test/latest',events:[
 {kind:'file',status:'success',epoch_ms:1000,title:'Changed file'},
 {kind:'test',status:'success',epoch_ms:1100,title:'Tests passed'},
 {kind:'commit',status:'success',epoch_ms:1200,title:'Commit'}
]}];
console.log(JSON.stringify(highwaySnapshot(units,'root-a',1500,1)));
"""
        )

        self.assertEqual(snapshot["combo"], 3)
        self.assertEqual(
            {mark["lane"] for mark in snapshot["marks"]},
            {"files", "tests", "git"},
        )
        self.assertTrue(all(not mark["url"] for mark in snapshot["marks"]))

    def test_highway_filters_pipeline_events_before_expansion(self) -> None:
        result = self.run_highway_logic(
            """
const units=[{id:'delivery',root:'root-a',events:[
 {kind:'file',status:'success',epoch_ms:1000,title:'Changed file'},
 {kind:'test',status:'success',epoch_ms:1100,title:'Tests passed'},
 {kind:'commit',status:'success',epoch_ms:1200,title:'Commit'}
]}];
console.log(JSON.stringify({files:highwaySnapshot(units,'root-a',1500,1,'files'),milestones:highwaySnapshot(units,'root-a',1500,1,'milestones')}));
"""
        )

        self.assertEqual(result["files"]["combo"], 1)
        self.assertEqual(
            {mark["lane"] for mark in result["files"]["marks"]}, {"files"}
        )
        self.assertEqual(result["milestones"]["combo"], 2)
        self.assertEqual(
            {mark["lane"] for mark in result["milestones"]["marks"]},
            {"tests", "git"},
        )

    def test_highway_hides_passive_filesystem_events_but_keeps_native_file_events(
        self,
    ) -> None:
        result = self.run_highway_logic(
            """
const units=[
 {id:'passive',root:'root-a',events:[{kind:'file',agent:'filesystem',status:'success',epoch_ms:1000,title:'Passive file'}]},
 {id:'native',root:'root-a',events:[{kind:'config',agent:'codex',status:'success',epoch_ms:1100,title:'Agent config'}]}
];
const snapshot=(filter,show)=>highwaySnapshot(units,'root-a',1500,1,filter,show).marks.map(mark=>mark.detail);
console.log(JSON.stringify({hidden:snapshot('all',false),hiddenFiles:snapshot('files',false),shown:snapshot('all',true)}));
"""
        )

        self.assertEqual(result["hidden"], ["Agent config"])
        self.assertEqual(result["hiddenFiles"], ["Agent config"])
        self.assertEqual(result["shown"], ["Passive file", "Agent config"])

    def test_highway_running_hold_uses_elapsed_time_and_speed(self) -> None:
        result = self.run_highway_logic(
            """
const running=[{id:'run',root:'root-a',events:[{kind:'push',status:'running',operation_id:'push-1',epoch_ms:1000,started_epoch_ms:1000,title:'Pushing'}]}];
const event=[{id:'old',root:'root-a',events:[{kind:'commit',status:'success',epoch_ms:1000,title:'Commit'}]}];
console.log(JSON.stringify({running:highwaySnapshot(running,'root-a',6000,1).marks[0],normal:highwaySnapshot(event,'root-a',6000,1).marks[0],fast:highwaySnapshot(event,'root-a',6000,2).marks[0]}));
"""
        )

        self.assertEqual(result["running"]["lane"], "git")
        self.assertEqual(result["running"]["y"], 0)
        self.assertEqual(result["running"]["hold"], 20)
        self.assertEqual(result["running"]["judgment"], "LIVE")
        self.assertEqual(result["fast"]["y"], result["normal"]["y"] * 2)
        self.assertTrue(result["normal"]["showJudgment"])
        self.assertFalse(result["fast"]["showJudgment"])

    def test_highway_staggers_dense_notes_without_changing_time_position(self) -> None:
        marks = self.run_highway_logic(
            """
const event=id=>({id,root:'root-a',events:[{kind:'file',status:'success',epoch_ms:1000,title:id}]});
console.log(JSON.stringify(highwaySnapshot([event('one'),event('two')],'root-a',10000,1).marks));
"""
        )

        self.assertEqual(marks[0]["y"], marks[1]["y"])
        self.assertNotEqual(marks[0]["offset"], marks[1]["offset"])
        self.assertFalse(marks[0]["showJudgment"])

    def test_highway_combo_treats_unknown_as_neutral(self) -> None:
        result = self.run_highway_logic(
            """
const event=(id,status,epoch)=>({id,root:'root-a',events:[{kind:'test',status,epoch_ms:epoch,title:id}]});
const neutral=[event('one','success',1000),event('unknown','unknown',1100),event('two','success',1200)];
const failed=[...neutral,event('miss','failed',1300)];
const recovered=[...failed,event('three','success',1400)];
console.log(JSON.stringify({neutral:highwaySnapshot(neutral,'root-a',1500,1).combo,failed:highwaySnapshot(failed,'root-a',1500,1).combo,recovered:highwaySnapshot(recovered,'root-a',1500,1).combo}));
"""
        )

        self.assertEqual(result, {"neutral": 2, "failed": 0, "recovered": 1})

    def test_highway_animation_stops_for_pause_and_reduced_motion(self) -> None:
        result = self.run_highway_logic(
            """
console.log(JSON.stringify({live:highwayShouldAnimate('highway',false,false),paused:highwayShouldAnimate('highway',true,false),reduced:highwayShouldAnimate('highway',false,true),timeline:highwayShouldAnimate('timeline',false,false)}));
"""
        )

        self.assertEqual(
            result,
            {"live": True, "paused": False, "reduced": False, "timeline": False},
        )
        self.assertIn("cancelAnimationFrame(highwayFrame)", PANEL_HTML)
        self.assertIn("if(animate&&highwayFrame===null)", PANEL_HTML)
        self.assertIn("frozenAt:motionQuery.matches?Date.now():null", PANEL_HTML)
        self.assertIn("data-mark-id", PANEL_HTML)
        self.assertIn("shell.querySelectorAll('.highway-note')", PANEL_HTML)
        self.assertNotIn("shell.innerHTML=highwayHTML", PANEL_HTML)

    def test_timeline_notice_reports_retained_sort_order(self) -> None:
        result = self.run_highway_logic(
            """
console.log(JSON.stringify({newest:timelineOrderNotice(true),oldest:timelineOrderNotice(false)}));
"""
        )

        self.assertIn("newest-first", result["newest"])
        self.assertIn("oldest-first", result["oldest"])
        self.assertIn("timelineOrderNotice(state.newest)", PANEL_HTML)

    def test_highway_freeze_timestamp_survives_overlapping_freeze_states(self) -> None:
        result = self.run_highway_logic(
            """
const initial=highwayFreezeTimestamp(false,true,null,1000);
const paused=highwayFreezeTimestamp(true,true,initial,2000);
const resumed=highwayFreezeTimestamp(false,true,paused,3000);
const moving=highwayFreezeTimestamp(false,false,resumed,4000);
console.log(JSON.stringify({initial,paused,resumed,moving}));
"""
        )

        self.assertEqual(
            result, {"initial": 1000, "paused": 1000, "resumed": 1000, "moving": None}
        )

    def test_html_notice_replaces_and_expires_without_modal_interaction(self) -> None:
        self.assertIn('role="status"', PANEL_HTML)
        self.assertIn('aria-live="polite"', PANEL_HTML)
        self.assertIn("const NOTICE_MS=2000", PANEL_HTML)
        self.assertIn("clearTimeout(noticeTimer)", PANEL_HTML)
        self.assertIn("noticeTimer=setTimeout", PANEL_HTML)
        self.assertIn("notice.hidden=true", PANEL_HTML)
        self.assertNotIn("alert(", PANEL_HTML)

    def test_html_explains_every_resulting_control_state(self) -> None:
        for explanation in (
            "Expanded — grouped file paths are open.",
            "Compact — grouped file paths are closed.",
            "Milestones only — commits, pushes, PRs, tests, branches.",
            "File writes only — everything else is hidden.",
            "Everything — file writes and milestones together.",
            "Paused — collection continues; display updates are held.",
            "Live — held updates are now visible.",
            "Newest first — new events appear at the top.",
            "Oldest first — new events appear at the bottom.",
            "Showing only ",
            "All folders — one column each.",
            "All folders — stacked one above the other.",
            "Automatic layout — folders use columns",
            "it stays full-width",
            "Columns view — the pane is too narrow to fit every folder, so the row scrolls sideways.",
            "Columns view — each folder has its own side-by-side list.",
            "Stacked view — each folder has its own full-width list.",
        ):
            self.assertIn(explanation, PANEL_HTML)

    def test_html_keyboard_controls_use_the_same_notice_actions_as_buttons(
        self,
    ) -> None:
        for binding in (
            "e.key==='e')toggleExpanded()",
            "e.key==='f')cycleFilter()",
            "e.key==='F')toggleFilesystemActivity()",
            "e.key==='p')togglePause()",
            "e.key==='r')toggleOrder()",
            "e.key==='a')showAllRoots()",
            "e.key==='Tab'",
            "focusRoot(Number(e.key)-1)",
        ):
            self.assertIn(binding, PANEL_HTML)
        self.assertIn("function columnsFit()", PANEL_HTML)
        self.assertIn("ROOT_PADDING_PX+count*ROOT_MIN_PX", PANEL_HTML)
        self.assertIn("Math.max(0,count-1)*ROOT_GAP_PX", PANEL_HTML)
        self.assertIn("if(state.focus)", PANEL_HTML)
        self.assertIn("if(e.ctrlKey||e.metaKey||e.altKey)return", PANEL_HTML)
        self.assertIn("return columnsFit()?'columns':'stack'", PANEL_HTML)
        self.assertNotIn("ResizeObserver", PANEL_HTML)
        self.assertIn(
            "window.addEventListener('resize',renderResponsiveChrome)", PANEL_HTML
        )

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

    def test_display_post_updates_and_remembers_the_filesystem_preference(self) -> None:
        feed = StubFeed()
        with patch("side_dog.panel.save_filesystem_activity_setting") as save:
            server = PanelServer(
                ("127.0.0.1", 0), "private-token", feed, 0.1  # type: ignore[arg-type]
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port
                )
                body = json.dumps({"show_filesystem_activity": True})
                connection.request(
                    "POST",
                    "/private-token/display",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()

                self.assertEqual(response.status, 200)
                self.assertEqual(payload, {"show_filesystem_activity": True})
                self.assertTrue(feed.show_filesystem_activity)
                save.assert_called_once_with(True)
                snapshot, _ = server.subscribe()
                self.assertEqual(
                    snapshot["display"], {"show_filesystem_activity": True}
                )
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
                    "root": str(root),
                    "working_root": str(root),
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
                patch("side_dog.panel.load_agent_identities", return_value=identity),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root])
                try:
                    snapshot = feed.snapshot()
                    self.assertEqual(snapshot["schema"], PANEL_SCHEMA)
                    self.assertEqual(snapshot["discovery_mode"]["key"], "explicit")
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

    def test_stale_github_refresh_is_hidden_after_branch_switch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            current_git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch(
                    "side_dog.panel.load_git_state", side_effect=lambda _: current_git
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root])
                try:
                    state = feed.roots[0]
                    completed: Future[tuple[dict[str, object], None]] = Future()
                    completed.set_result(({"number": 1, "state": "OPEN"}, None))
                    state.github = {"number": 1, "state": "OPEN"}
                    state.github_branch = "main"
                    state.github_refresh = completed
                    state.github_refresh_branch = "main"

                    feed._collect_external_refreshes()

                    self.assertIsNone(feed._wire_root(state)["github"])
                    self.assertNotEqual(state.github_branch, "feature")
                finally:
                    feed.close()

    def test_transient_github_failure_preserves_active_pr_and_poll_cadence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", return_value=git),
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch("side_dog.panel.load_github_pr", return_value=(None, None)),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    state = feed.roots[0]
                    verified = {"number": 67, "state": "OPEN"}
                    completed: Future[tuple[None, str]] = Future()
                    completed.set_result((None, "failed to connect to api.github.com"))
                    state.github = verified
                    state.github_branch = "feature"
                    state.github_refresh = completed
                    state.github_refresh_branch = "feature"
                    state.last_github_refresh = 100.0

                    feed._collect_external_refreshes()
                    feed._start_external_refreshes(161.0)

                    self.assertEqual(state.github, verified)
                    self.assertEqual(state.github_branch, "feature")
                    self.assertIsNotNone(state.github_refresh)
                finally:
                    feed.close()

    def test_transient_first_refresh_remembers_the_queried_branch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = {"branch": "main", "oid": "111", "short_oid": "111"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", side_effect=lambda _: git),
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch(
                    "side_dog.panel.load_github_pr",
                    return_value=(None, "failed to connect to api.github.com"),
                ),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    state = feed.roots[0]
                    feed._start_external_refreshes(100.0)
                    deadline = time.monotonic() + 1.0
                    while (
                        state.github_refresh is not None
                        and not state.github_refresh.done()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    feed._collect_external_refreshes()

                    self.assertIsNone(state.github)
                    self.assertIsNone(state.github_branch)
                    self.assertEqual(state.github_query_branch, "main")

                    git["branch"] = "feature"
                    feed._refresh_git_states()
                    feed._start_external_refreshes(101.0)

                    self.assertIsNotNone(state.github_refresh)
                    self.assertEqual(state.github_refresh_branch, "feature")
                    self.assertEqual(state.github_query_branch, "feature")
                finally:
                    feed.close()

    def test_definitive_no_pr_clears_verified_panel_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", return_value=git),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    state = feed.roots[0]
                    completed: Future[tuple[None, str]] = Future()
                    completed.set_result(
                        (None, "no pull requests found for branch feature")
                    )
                    state.github = {"number": 67, "state": "OPEN"}
                    state.github_branch = "feature"
                    state.github_refresh = completed
                    state.github_refresh_branch = "feature"

                    feed._collect_external_refreshes()

                    self.assertIsNone(state.github)
                    self.assertEqual(state.github_branch, "feature")
                finally:
                    feed.close()

    def test_repeated_snapshots_do_not_force_github_refreshes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", return_value=git),
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch(
                    "side_dog.panel.load_github_pr", return_value=(None, None)
                ) as load_github,
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root])
                try:
                    feed.snapshot()
                    deadline = time.monotonic() + 1.0
                    while (
                        feed.roots[0].github_refresh is not None
                        and not feed.roots[0].github_refresh.done()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    feed._collect_external_refreshes()

                    feed.snapshot()

                    self.assertEqual(load_github.call_count, 1)
                finally:
                    feed.close()

    def test_poll_loads_git_state_once_per_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", return_value=git) as load_git,
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch("side_dog.panel.load_github_pr", return_value=(None, None)),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    load_git.reset_mock()

                    feed.poll()

                    load_git.assert_called_once_with(root)
                finally:
                    feed.close()

    def test_branch_switch_bypasses_terminal_pr_backoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=root / "events.jsonl"),
                patch("side_dog.panel.load_git_state", return_value=git),
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch("side_dog.panel.load_github_pr", return_value=(None, None)),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    state = feed.roots[0]
                    state.github = {"number": 1, "state": "MERGED"}
                    state.github_branch = "main"
                    state.last_github_refresh = 100.0

                    feed._start_external_refreshes(101.0)

                    self.assertIsNotNone(state.github_refresh)
                    self.assertEqual(state.github_refresh_branch, "feature")
                finally:
                    feed.close()

    def test_pr_event_bypasses_no_pr_backoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            event_log = root / "events.jsonl"
            event_log.write_text("")
            git = {"branch": "feature", "oid": "222", "short_oid": "222"}
            with (
                patch("side_dog.panel.events_path", return_value=event_log),
                patch("side_dog.panel.load_git_state", return_value=git),
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch(
                    "side_dog.panel.load_github_pr", return_value=(None, None)
                ) as load_github,
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root], follow_worktrees=False)
                try:
                    state = feed.roots[0]
                    state.github = None
                    state.github_branch = "feature"
                    state.last_github_refresh = time.monotonic()
                    with event_log.open("a") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "schema": SCHEMA,
                                    "agent": "github",
                                    "kind": "pr",
                                    "status": "running",
                                    "title": "Opening pull request",
                                    "detail": "gh pr create",
                                }
                            )
                            + "\n"
                        )

                    feed.poll()
                    deadline = time.monotonic() + 1.0
                    while load_github.call_count == 0 and time.monotonic() < deadline:
                        time.sleep(0.01)

                    self.assertEqual(load_github.call_count, 1)
                    self.assertEqual(state.github_refresh_branch, "feature")
                finally:
                    feed.close()

    def test_feed_replaces_snapshot_when_retained_units_are_evicted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            event_log = Path(directory) / "events.jsonl"

            def record(epoch: int, detail: str) -> dict[str, object]:
                return {
                    "schema": SCHEMA,
                    "epoch_ms": epoch,
                    "kind": "file",
                    "title": "File changed",
                    "detail": detail,
                    "agent": "filesystem",
                    "status": "success",
                }

            old = record(1_000, "old.py")
            event_log.write_text(json.dumps(old) + "\n")
            with (
                patch("side_dog.panel.events_path", return_value=event_log),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel.load_github_pr", return_value=(None, None)),
                patch("side_dog.panel.load_agent_identities", return_value={}),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root])
                try:
                    feed.roots[0].records = deque([old], maxlen=1)
                    first = feed.snapshot()
                    old_id = first["units"][0]["id"]
                    with event_log.open("a") as handle:
                        handle.write(json.dumps(record(100_000, "new.py")) + "\n")

                    updates = feed.poll()

                    snapshots = [
                        value for event, value in updates if event == "snapshot"
                    ]
                    self.assertEqual(len(snapshots), 1)
                    self.assertNotIn(
                        old_id, {unit["id"] for unit in snapshots[0]["units"]}
                    )
                    self.assertEqual(
                        snapshots[0]["units"][0]["events"][0]["detail"], "new.py"
                    )
                finally:
                    feed.close()
