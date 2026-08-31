import http.client
import json
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
from unittest.mock import patch

from side_dog.cli import SCHEMA, build_parser
from side_dog.panel import (
    PANEL_HTML,
    PANEL_HIGHWAY_LOGIC_JS,
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
        self.assertIn("h highway", PANEL_HTML)
        self.assertIn("s 1×", PANEL_HTML)

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
            "Expanded history — grouped filesystem paths are open.",
            "Compact history — grouped filesystem paths are closed.",
            "Milestones only — file activity is hidden.",
            "Files only — delivery milestones are hidden.",
            "All activity — files and delivery milestones are visible.",
            "Paused — collection continues; display updates are held.",
            "Live — held updates are now visible.",
            "Newest first — new events appear at the top.",
            "Oldest first — new events appear at the bottom.",
            "Focused root:",
            "All roots — showing one column per root.",
            "All roots — showing stacked root timelines.",
            "Automatic layout — roots use columns",
            "focus stays full-width",
            "Columns requested — the pane is too narrow",
            "Columns view — each root has its own side-by-side timeline.",
            "Stacked view — each root has its own full-width timeline.",
        ):
            self.assertIn(explanation, PANEL_HTML)

    def test_html_keyboard_controls_use_the_same_notice_actions_as_buttons(
        self,
    ) -> None:
        for binding in (
            "e.key==='e')toggleExpanded()",
            "e.key==='f')cycleFilter()",
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
        self.assertIn(
            "new ResizeObserver(()=>{document.body.className=bodyClass()})",
            PANEL_HTML,
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

    def test_feed_replaces_snapshot_when_retained_units_are_evicted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            event_log = Path(directory) / "events.jsonl"

            def record(epoch: int, detail: str) -> dict[str, object]:
                return {
                    "schema": SCHEMA,
                    "epoch_ms": epoch,
                    "timestamp": "1970-01-01T00:00:01+00:00",
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
                patch("side_dog.panel.load_herdr_identities", return_value={}),
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
