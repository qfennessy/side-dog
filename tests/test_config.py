from __future__ import annotations

import io
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    STATE_ENV,
    WATCH_DEFAULT_PROJECTS,
    WATCH_ROOT_LIMIT,
    agent_working_folders,
    append_event,
    build_parser,
    busy_worktrees,
    canonical_root,
    discovered_watch_roots,
    discovered_worktrees,
    display_settings_path,
    follow_new_worktrees,
    load_display_settings,
    main,
    pinned_folders,
    herdr_identities_for_root,
    keep_one_root,
    rediscovered_roots,
    render,
    space_folders,
    watch_repository_context,
    retired_worktrees,
    save_display_settings,
    watch,
    watch_root_limit,
)
from side_dog.config import (
    CONFIG_HOME_ENV,
    config_display,
    config_home,
    config_limit,
    config_path,
    expand_path,
    load_config,
    migrate_display_settings,
    load_spaces,
    path_is_ignored,
    read_toml,
    save_space,
    spaces_path,
)
from tests.test_multi_root import root_state


class TtyStream(io.StringIO):
    """A stdout stand-in that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


@contextmanager
def sandbox(config: str | None = None) -> Iterator[Path]:
    """A private configuration and state directory, with an optional file."""
    with TemporaryDirectory() as directory:
        home = Path(directory) / "config"
        with patch.dict(
            os.environ,
            {
                CONFIG_HOME_ENV: os.fspath(home),
                STATE_ENV: directory + "/state",
                # Isolate Pi's session store the way each test isolates Codex's,
                # so a live Pi session on the host machine cannot leak in.
                "PI_CODING_AGENT_DIR": directory + "/pi-agent",
                # Keep opencode's real session store out of discovery tests.
                "XDG_DATA_HOME": directory + "/data",
                # Keep Cline's real shared session store out of discovery tests.
                "CLINE_DATA_DIR": directory + "/cline-data",
            },
        ):
            config_path().parent.mkdir(parents=True, exist_ok=True)
            if config is not None:
                config_path().write_text(config)
            yield Path(directory)


def render_watch(projects: object, **overrides: object) -> str:
    """One frame of the watcher, with the terminal and Herdr stood in for."""
    stream = TtyStream()
    with (
        patch("side_dog.cli.sys.stdout", stream),
        patch("side_dog.cli.sys.stdin", stream),
    ):
        watch(
            projects,
            width=80,
            poll=0.0,
            no_color=True,
            github_poll=0.0,
            once=True,
            **overrides,
        )
    return stream.getvalue()


def render_once(root: Path) -> str:
    with patch("side_dog.cli.load_herdr_identities", return_value={}):
        return render_watch(os.fspath(root))


class ConfigLocationTest(TestCase):
    def test_the_file_lives_under_xdg_config_home_when_it_is_set(self) -> None:
        with patch.dict(os.environ, {CONFIG_HOME_ENV: "/elsewhere"}):
            self.assertEqual(config_home(), Path("/elsewhere/side-dog"))
            self.assertEqual(config_path(), Path("/elsewhere/side-dog/config.toml"))

    def test_the_file_falls_back_to_the_dot_config_folder(self) -> None:
        environment = dict(os.environ)
        environment.pop(CONFIG_HOME_ENV, None)
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(config_home(), Path.home() / ".config" / "side-dog")


class MissingAndMalformedTest(TestCase):
    def test_no_configuration_file_changes_nothing(self) -> None:
        with sandbox() as directory:
            self.assertFalse(config_path().exists())
            self.assertEqual(load_config(), {})
            self.assertEqual(config_display(load_config()), {})
            self.assertEqual(watch_root_limit(), WATCH_ROOT_LIMIT)

            project = directory / "project"
            project.mkdir()
            self.assertIn("SIDE DOG", render_once(project))

    def test_a_malformed_file_is_ignored_rather_than_fatal(self) -> None:
        with sandbox("[display\norder = not-even-a-string") as directory:
            self.assertEqual(load_config(), {})
            self.assertEqual(watch_root_limit(), WATCH_ROOT_LIMIT)

            project = directory / "project"
            project.mkdir()
            self.assertIn("SIDE DOG", render_once(project))

    def test_a_file_that_cannot_be_read_is_ignored(self) -> None:
        with sandbox() as directory:
            unreadable = directory / "config" / "side-dog"
            unreadable.mkdir(parents=True, exist_ok=True)
            # A folder where a file should be fails to open, like a bad mode.
            (unreadable / "config.toml").mkdir()

            self.assertEqual(load_config(), {})

    def test_a_document_that_is_not_a_table_of_values_is_ignored(self) -> None:
        with sandbox("pin = [1, 2, 3]\n[display]\nlimit = 'eight'\n"):
            document = load_config()
            self.assertEqual(config_limit(document, WATCH_ROOT_LIMIT), 8)
            self.assertEqual(config_display(document), {})


class ExpansionTest(TestCase):
    def test_home_and_environment_variables_expand(self) -> None:
        with patch.dict(os.environ, {"SIDE_DOG_TEST_WORK": "/work/checkouts"}):
            self.assertEqual(
                expand_path("$SIDE_DOG_TEST_WORK/project"), "/work/checkouts/project"
            )
        self.assertEqual(expand_path("~/src"), os.fspath(Path.home() / "src"))

    def test_an_unset_variable_is_left_alone_rather_than_emptied(self) -> None:
        self.assertEqual(expand_path("$NOT_SET_ANYWHERE/x"), "$NOT_SET_ANYWHERE/x")


class LimitTest(TestCase):
    def test_limit_replaces_the_built_in_cap(self) -> None:
        with sandbox("[display]\nlimit = 3\n"):
            self.assertEqual(watch_root_limit(), 3)

    def test_a_nonsense_limit_keeps_the_built_in_cap(self) -> None:
        for value in ("0", "-2", "true", '"many"'):
            with self.subTest(value=value):
                with sandbox(f"[display]\nlimit = {value}\n"):
                    self.assertEqual(watch_root_limit(), WATCH_ROOT_LIMIT)


class DisplayDefaultsTest(TestCase):
    def test_the_file_is_translated_into_the_names_the_watcher_uses(self) -> None:
        with sandbox(
            '[display]\norder = "oldest"\ndetail = "expanded"\nfilter = "files"\n'
        ):
            self.assertEqual(
                config_display(load_config()),
                {
                    "newest_first": False,
                    "expanded_history": True,
                    "event_filter": "files",
                },
            )

    def test_an_unrecognized_value_leaves_the_watcher_default_alone(self) -> None:
        with sandbox('[display]\norder = "sideways"\ndetail = 3\n'):
            self.assertEqual(config_display(load_config()), {})

    def test_the_saved_toggles_win_over_the_file(self) -> None:
        with sandbox('[display]\norder = "oldest"\ndetail = "expanded"\n'):
            save_display_settings(
                newest_first=True, expanded_history=False, event_filter="milestones"
            )
            starting = {**config_display(load_config()), **load_display_settings()}

            self.assertEqual(
                starting,
                {
                    "newest_first": True,
                    "expanded_history": False,
                    "event_filter": "milestones",
                },
            )


class MigrationTest(TestCase):
    def test_remembered_toggles_are_copied_into_a_first_file(self) -> None:
        with sandbox():
            save_display_settings(
                newest_first=False, expanded_history=True, event_filter="files"
            )
            saved = load_display_settings()
            self.assertTrue(migrate_display_settings(saved))

            written = read_toml(config_path())
            self.assertEqual(
                written["display"],
                {"order": "oldest", "detail": "expanded", "filter": "files"},
            )
            # Nothing the user had is taken away.
            self.assertTrue(display_settings_path().exists())

    def test_an_existing_file_is_never_overwritten(self) -> None:
        with sandbox('[display]\nfilter = "all"\n'):
            save_display_settings(
                newest_first=False, expanded_history=True, event_filter="files"
            )
            self.assertFalse(migrate_display_settings(load_display_settings()))
            self.assertEqual(load_config()["display"], {"filter": "all"})

    def test_nothing_remembered_writes_nothing(self) -> None:
        with sandbox():
            self.assertFalse(migrate_display_settings({}))
            self.assertFalse(config_path().exists())

    def test_watch_migrates_on_the_way_past(self) -> None:
        with sandbox() as directory:
            save_display_settings(
                newest_first=False, expanded_history=False, event_filter="milestones"
            )
            project = directory / "project"
            project.mkdir()

            render_once(project)

            self.assertEqual(
                read_toml(config_path())["display"],
                {"order": "oldest", "detail": "compact", "filter": "milestones"},
            )


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def repository(directory: Path) -> Path:
    main = directory / "project"
    main.mkdir()
    git(main, "init", "-b", "main")
    git(main, "config", "user.email", "side-dog@example.com")
    git(main, "config", "user.name", "Side Dog")
    (main / "README.md").write_text("start\n")
    git(main, "add", "README.md")
    git(main, "commit", "-m", "start")
    return main


class PinTest(TestCase):
    def test_a_pinned_folder_is_watched_without_being_named(self) -> None:
        with sandbox() as directory:
            project = directory / "project"
            project.mkdir()
            always = directory / "always-here"
            always.mkdir()
            config_path().write_text(f'pin = ["{always}"]\n')

            output = render_once(project)

            self.assertIn("always-here", output)

    def test_a_pin_that_points_nowhere_is_skipped_rather_than_fatal(self) -> None:
        with sandbox() as directory:
            here = directory / "here"
            here.mkdir()
            config_path().write_text(
                f'pin = ["{here}", "{directory / "not-on-this-machine"}"]\n'
            )

            self.assertEqual(pinned_folders(load_config()), [canonical_root(here)])

    def test_a_pinned_folder_survives_retirement(self) -> None:
        now = int(time.time() * 1000)
        with sandbox() as directory:
            main = repository(directory)
            branch = directory / "project-landed"
            git(main, "worktree", "add", os.fspath(branch), "-b", "landed")
            landed = canonical_root(branch)
            root = canonical_root(main)
            with patch("side_dog.cli.load_herdr_identities", return_value={}):
                append_event(
                    landed,
                    {
                        "agent": "github",
                        "kind": "github",
                        "status": "success",
                        "title": "PR #7 merged",
                        "detail": "landed",
                        "github": {"number": 7, "state": "MERGED"},
                    },
                )
                states = [root_state(root, []), root_state(landed, [])]

                # Nothing pinned: a finished worktree gives the pane its room back.
                self.assertEqual(retired_worktrees(states, {root}, set()), [landed])

                config_path().write_text(f'pin = ["{landed}"]\n')
                self.assertEqual(retired_worktrees(states, {root}, set()), [])
                self.assertEqual(busy_worktrees([root], now, 8), [])


class IgnoreTest(TestCase):
    def test_ignore_beats_a_busy_folder(self) -> None:
        now = int(time.time() * 1000)
        with sandbox() as directory:
            main = repository(directory)
            branch = directory / "project-hot"
            git(main, "worktree", "add", os.fspath(branch), "-b", "hot")
            root = canonical_root(main)
            hot = canonical_root(branch)
            with patch("side_dog.cli.load_herdr_identities", return_value={}):
                # A worktree with a commit a moment ago is as busy as they get.
                self.assertEqual(busy_worktrees([root], now, 8), [hot])
                self.assertIn(hot, discovered_worktrees([root]))

                resolved = canonical_root(directory)
                config_path().write_text(f'ignore = ["{resolved}/project-*"]\n')

                self.assertEqual(busy_worktrees([root], now, 8), [])
                self.assertNotIn(hot, discovered_worktrees([root]))
                states = [root_state(root, [])]
                known = discovered_worktrees([root]) | {root}
                additions, _ = follow_new_worktrees(states, known, now)
                self.assertEqual(additions, [])

    def test_a_pattern_star_covers_everything_underneath(self) -> None:
        patterns = ["/home/q/.codex/worktrees/*"]

        self.assertTrue(path_is_ignored("/home/q/.codex/worktrees/0e41", patterns))
        self.assertTrue(
            path_is_ignored("/home/q/.codex/worktrees/0e41/deep/inside", patterns)
        )
        self.assertFalse(path_is_ignored("/home/q/.codex/worktrees", patterns))
        self.assertFalse(path_is_ignored("/home/q/src/project", patterns))

    def test_a_folder_named_on_the_command_line_is_never_ignored(self) -> None:
        with sandbox() as directory:
            project = directory / "project"
            project.mkdir()
            config_path().write_text(f'ignore = ["{canonical_root(directory)}/*"]\n')

            self.assertIn("project", render_once(project))


def herdr_agent(cwd: Path, status: str = "idle", workspace: str = "w1") -> dict:
    return {
        "agent": "codex",
        "agent_status": status,
        "cwd": os.fspath(cwd),
        "foreground_cwd": os.fspath(cwd),
        "pane_id": f"{workspace}:p1",
        "workspace_id": workspace,
        "tab_id": f"{workspace}:t1",
        "terminal_title": "work",
        "agent_session": {"value": f"session-{workspace}"},
    }


class DiscoveryTest(TestCase):
    def test_every_source_contributes_the_folder_its_agent_is_in(self) -> None:
        with sandbox() as directory:
            herdr = directory / "from-herdr"
            claude = directory / "from-claude"
            codex = directory / "from-codex"
            for folder in (herdr, claude, codex):
                folder.mkdir()
            session = directory / "codex-session.jsonl"
            session.write_text("{}\n")
            with (
                patch(
                    "side_dog.cli.herdr_snapshot",
                    return_value={"agents": [herdr_agent(herdr, "working")]},
                ),
                patch(
                    "side_dog.cli.claude_session_registry",
                    return_value=[{"sessionId": "abc", "pid": 1, "cwd": os.fspath(claude)}],
                ),
                patch("side_dog.cli.claude_session_status", return_value="idle"),
                patch(
                    # Written five minutes ago: recent enough to count as an
                    # agent, quiet enough not to count as working.
                    "side_dog.cli.codex_recent_sessions",
                    return_value=[(session, time.time() - 300)],
                ),
                patch(
                    "side_dog.cli.codex_session_header",
                    return_value={"cwd": os.fspath(codex), "id": "codex-1"},
                ),
            ):
                found = agent_working_folders()
                roots = discovered_watch_roots()

            self.assertEqual(
                set(found),
                {canonical_root(herdr), canonical_root(claude), canonical_root(codex)},
            )
            # A working agent is named first, so the cap drops the quiet ones.
            self.assertEqual(roots[0], canonical_root(herdr))
            self.assertEqual(len(roots), 3)

    def test_a_helper_thread_is_not_a_folder_to_watch(self) -> None:
        with sandbox() as directory:
            worker = directory / "worker"
            worker.mkdir()
            session = directory / "worker-session.jsonl"
            session.write_text("{}\n")
            with (
                patch("side_dog.cli.herdr_snapshot", return_value={}),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch(
                    "side_dog.cli.codex_recent_sessions",
                    return_value=[(session, time.time())],
                ),
                patch(
                    "side_dog.cli.codex_session_header",
                    return_value={
                        "cwd": os.fspath(worker),
                        "id": "worker-1",
                        "thread_source": "subagent",
                    },
                ),
            ):
                self.assertEqual(agent_working_folders(), {})

    def test_discovery_drops_ignored_folders_and_keeps_pins(self) -> None:
        with sandbox() as directory:
            busy = directory / "busy"
            pinned = directory / "pinned"
            for folder in (busy, pinned):
                folder.mkdir()
            resolved = canonical_root(directory)
            config_path().write_text(
                f'pin = ["{canonical_root(pinned)}"]\n'
                f'ignore = ["{resolved}/busy"]\n'
            )
            with (
                patch(
                    "side_dog.cli.herdr_snapshot",
                    return_value={"agents": [herdr_agent(busy, "working")]},
                ),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
            ):
                self.assertEqual(discovered_watch_roots(), [canonical_root(pinned)])

    def test_the_limit_caps_what_discovery_returns(self) -> None:
        with sandbox() as directory:
            config_path().write_text("[display]\nlimit = 2\n")
            folders = []
            for name in ("a", "b", "c"):
                folder = directory / name
                folder.mkdir()
                folders.append(folder)
            with (
                patch(
                    "side_dog.cli.herdr_snapshot",
                    return_value={
                        "agents": [
                            herdr_agent(folder, "idle", f"w{index}")
                            for index, folder in enumerate(folders)
                        ]
                    },
                ),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
            ):
                self.assertEqual(
                    discovered_watch_roots(),
                    [canonical_root(folders[0]), canonical_root(folders[1])],
                )

    def test_watch_with_no_folders_watches_what_discovery_found(self) -> None:
        with sandbox() as directory:
            working = directory / "where-the-work-is"
            working.mkdir()
            with (
                patch(
                    "side_dog.cli.herdr_snapshot",
                    return_value={"agents": [herdr_agent(working, "working")]},
                ),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
            ):
                output = render_watch([])

            self.assertIn("where-the-work-is", output)

    def test_a_pin_discovery_also_found_is_watched_once(self) -> None:
        with sandbox() as directory:
            both = directory / "pinned-and-busy"
            both.mkdir()
            config_path().write_text(f'pin = ["{canonical_root(both)}"]\n')
            with (
                patch(
                    "side_dog.cli.herdr_snapshot",
                    return_value={"agents": [herdr_agent(both, "working")]},
                ),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
            ):
                output = render_watch([])

            # One folder, so the header names it rather than counting several.
            self.assertIn("pinned-and-busy", output)
            self.assertNotIn("several folders", output)

    def test_watch_falls_back_to_the_current_folder(self) -> None:
        with sandbox() as directory:
            here = directory / "standing-here"
            here.mkdir()
            with (
                patch("side_dog.cli.herdr_snapshot", return_value={}),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
                patch("side_dog.cli.Path.cwd", return_value=here),
            ):
                previous = os.getcwd()
                os.chdir(here)
                try:
                    output = render_watch([])
                finally:
                    os.chdir(previous)

            self.assertIn("standing-here", output)

    def test_a_bare_watch_is_told_apart_from_watch_dot(self) -> None:
        parser = build_parser()

        # The parsed value still reads as the current folder, so anything
        # printing the arguments sees what it always did.
        self.assertEqual(parser.parse_args(["watch"]).projects, ["."])
        self.assertIs(parser.parse_args(["watch"]).projects, WATCH_DEFAULT_PROJECTS)
        self.assertIsNot(
            parser.parse_args(["watch", "."]).projects, WATCH_DEFAULT_PROJECTS
        )

    def test_the_command_line_passes_no_folders_through_to_watch(self) -> None:
        with patch("side_dog.cli.watch", return_value=0) as watching:
            main(["watch", "--once"])
            self.assertEqual(watching.call_args.args[0], [])

            main(["watch", ".", "--once"])
            self.assertEqual(watching.call_args.args[0], ["."])


def herdr_workspace(label: str, workspace: str) -> dict:
    return {
        "workspace_id": workspace,
        "label": label,
        "agent_status": "working",
        "pane_count": 1,
        "tab_count": 1,
    }


class NamedSpaceTest(TestCase):
    def snapshot(self, folders: dict[str, Path]) -> dict:
        return {
            "agents": [
                herdr_agent(folder, "working", f"w{index}")
                for index, folder in enumerate(folders.values())
            ],
            "workspaces": [
                herdr_workspace(label, f"w{index}")
                for index, label in enumerate(folders)
            ],
        }

    def test_a_label_resolves_to_the_folders_its_agents_are_in(self) -> None:
        with sandbox() as directory:
            cocos = directory / "cocos-story"
            other = directory / "pr-agent"
            for folder in (cocos, other):
                folder.mkdir()
            snapshot = self.snapshot({"cocos-story": cocos, "pr-agent": other})
            with patch("side_dog.cli.herdr_snapshot", return_value=snapshot):
                self.assertEqual(space_folders("cocos-story"), [canonical_root(cocos)])
                # Names are matched however they are capitalized.
                self.assertEqual(space_folders("COCOS-Story"), [canonical_root(cocos)])

    def test_an_unknown_name_lists_the_names_that_do_exist(self) -> None:
        with sandbox() as directory:
            cocos = directory / "cocos-story"
            cocos.mkdir()
            snapshot = self.snapshot({"cocos-story": cocos})
            with patch("side_dog.cli.herdr_snapshot", return_value=snapshot):
                with self.assertRaises(SystemExit) as raised:
                    space_folders("cocoa-story")

            message = str(raised.exception)
            self.assertIn("no space called 'cocoa-story'", message)
            self.assertIn("cocos-story", message)

    def test_a_name_two_spaces_share_is_refused_rather_than_guessed(self) -> None:
        with sandbox() as directory:
            first = directory / "side-dog"
            second = directory / "side-dog-startup"
            for folder in (first, second):
                folder.mkdir()
            snapshot = {
                "agents": [
                    herdr_agent(first, "working", "wB"),
                    herdr_agent(second, "working", "wD"),
                ],
                "workspaces": [
                    herdr_workspace("side-dog", "wB"),
                    herdr_workspace("side-dog", "wD"),
                ],
            }
            with patch("side_dog.cli.herdr_snapshot", return_value=snapshot):
                with self.assertRaises(SystemExit) as raised:
                    space_folders("side-dog")

            message = str(raised.exception)
            self.assertIn("2 spaces are called 'side-dog'", message)
            self.assertIn("--save", message)

    def test_a_space_with_nobody_in_it_says_so(self) -> None:
        with sandbox():
            snapshot = {"agents": [], "workspaces": [herdr_workspace("empty", "w1")]}
            with patch("side_dog.cli.herdr_snapshot", return_value=snapshot):
                with self.assertRaisesRegex(SystemExit, "no agent is working"):
                    space_folders("empty")

    def test_save_round_trips_through_the_file(self) -> None:
        with sandbox() as directory:
            first = directory / "project"
            second = directory / "project-issue-42"
            for folder in (first, second):
                folder.mkdir()

            with patch("side_dog.cli.herdr_snapshot", return_value={}):
                output = render_watch(
                    [os.fspath(first), os.fspath(second)], save_space_as="review"
                )
                self.assertIn("Saved 2 folders as @review", output)

                self.assertEqual(
                    space_folders("review"),
                    [canonical_root(first), canonical_root(second)],
                )
                restored = render_watch(["@review"])

            self.assertIn("project-issue-42", restored)
            self.assertIn(
                'review = ["', spaces_path().read_text()
            )

    def test_a_saved_name_wins_over_a_herdr_label(self) -> None:
        with sandbox() as directory:
            saved = directory / "saved-folder"
            herdr = directory / "herdr-folder"
            for folder in (saved, herdr):
                folder.mkdir()
            save_space("cocos-story", [os.fspath(saved)])
            snapshot = self.snapshot({"cocos-story": herdr})

            with patch("side_dog.cli.herdr_snapshot", return_value=snapshot):
                self.assertEqual(space_folders("cocos-story"), [canonical_root(saved)])

    def test_saving_twice_replaces_rather_than_duplicates(self) -> None:
        with sandbox() as directory:
            first = directory / "first"
            second = directory / "second"
            for folder in (first, second):
                folder.mkdir()

            save_space("keep", [os.fspath(first)])
            save_space("KEEP", [os.fspath(second)])

            self.assertEqual(list(load_spaces()), ["KEEP"])
            self.assertEqual(space_folders("keep"), [canonical_root(second)])

    def test_hand_written_spaces_in_the_configuration_file_are_read(self) -> None:
        with sandbox() as directory:
            folder = directory / "by-hand"
            folder.mkdir()
            config_path().write_text(
                "# a comment that must survive being read\n"
                f'[spaces]\nby-hand = ["{folder}"]\n'
            )

            self.assertEqual(space_folders("by-hand"), [canonical_root(folder)])
            # Saving another name leaves the hand-written file untouched.
            save_space("elsewhere", [os.fspath(folder)])
            self.assertIn("must survive", config_path().read_text())

    def test_names_that_are_not_bare_words_survive_the_writer(self) -> None:
        with sandbox() as directory:
            folder = directory / "odd"
            folder.mkdir()
            save_space('one "two" three', [os.fspath(folder)])

            self.assertEqual(
                space_folders('one "two" three'), [canonical_root(folder)]
            )

    def test_a_name_with_an_emoji_survives_the_writer(self) -> None:
        with sandbox() as directory:
            folder = directory / "fun"
            folder.mkdir()
            save_space("review \U0001f600", [os.fspath(folder)])

            self.assertEqual(
                space_folders("review \U0001f600"), [canonical_root(folder)]
            )

    def test_a_saved_name_beats_a_hand_written_one_whatever_the_case(self) -> None:
        with sandbox() as directory:
            written = directory / "by-hand"
            saved = directory / "saved"
            for folder in (written, saved):
                folder.mkdir()
            config_path().write_text(f'[spaces]\nReview = ["{written}"]\n')

            save_space("review", [os.fspath(saved)])

            self.assertEqual(space_folders("review"), [canonical_root(saved)])
            self.assertEqual(space_folders("Review"), [canonical_root(saved)])

    def test_the_parser_takes_a_name_to_save_under(self) -> None:
        parsed = build_parser().parse_args(["watch", ".", "--save", "review"])

        self.assertEqual(parsed.save_space_as, "review")
        self.assertIsNone(build_parser().parse_args(["watch"]).save_space_as)


class RetirementAcrossRepositoriesTest(TestCase):
    def test_a_landed_folder_keeps_its_place_while_an_agent_is_in_it(self) -> None:
        with sandbox() as directory:
            # Two repositories, because that is what discovery watches and what
            # the per-repository agent lookup cannot see across.
            first = repository(directory)
            second = directory / "zzz-other"
            second.mkdir()
            git(second, "init", "-b", "main")
            git(second, "config", "user.email", "side-dog@example.com")
            git(second, "config", "user.name", "Side Dog")
            (second / "README.md").write_text("start\n")
            git(second, "add", "README.md")
            git(second, "commit", "-m", "start")
            landed = canonical_root(second)
            append_event(
                landed,
                {
                    "agent": "github",
                    "kind": "github",
                    "status": "success",
                    "title": "PR #7 merged",
                    "detail": "landed",
                    "github": {"number": 7, "state": "MERGED"},
                },
            )
            snapshot = {
                "agents": [
                    herdr_agent(first, "working", "w1"),
                    herdr_agent(second, "working", "w2"),
                ],
                "workspaces": [],
            }
            with (
                patch("side_dog.cli.herdr_snapshot", return_value=snapshot),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
            ):
                output = render_watch([])

            # Both folders survive the first worktree scan: the landed one is
            # still occupied, even though it is in the other repository.
            # Discovery chose these folders, and the pane says so.
            self.assertIn("Watching 2 found folders", output)
            self.assertIn("PR #7", output)


class KeepOneRootTest(TestCase):
    def test_the_last_folder_is_never_retired(self) -> None:
        only = Path("/tmp/only")
        # A bare `side-dog watch` requests nothing, so when its one discovered
        # folder finishes, retirement would empty the pane - and the frame
        # after that has no folder to draw. The quietest folder keeps its seat.
        self.assertEqual(keep_one_root([only], 1), [])
        self.assertEqual(
            keep_one_root([Path("/tmp/a"), Path("/tmp/b")], 2), [Path("/tmp/a")]
        )
        self.assertEqual(keep_one_root([only], 3), [only])
        self.assertEqual(keep_one_root([], 0), [])


class FollowUpReviewTest(TestCase):
    """The second and third Codex passes on PR #38, fixed after it merged."""

    def test_an_agent_starting_in_a_new_repository_is_rediscovered(self) -> None:
        with sandbox() as directory:
            first = directory / "already-watched"
            second = directory / "started-later"
            for folder in (first, second):
                folder.mkdir()
            states = [root_state(canonical_root(first), [])]

            with patch(
                "side_dog.cli.agent_working_folders",
                return_value={canonical_root(first): True, canonical_root(second): True},
            ):
                retired, added = rediscovered_roots(states, load_config(), 8, set())

            self.assertEqual(added, [canonical_root(second)])
            self.assertEqual(retired, [])

    def test_a_new_repository_displaces_an_idle_folder_at_the_cap(self) -> None:
        with sandbox() as directory:
            idle = directory / "idle"
            busy = directory / "busy"
            newcomer = directory / "newcomer"
            for folder in (idle, busy, newcomer):
                folder.mkdir()
            states = [
                root_state(canonical_root(idle), []),
                root_state(canonical_root(busy), []),
            ]

            # Both seats are taken and the newcomer's agent is working; the
            # idle folder, absent from discovery's answer, gives up its seat.
            with patch(
                "side_dog.cli.agent_working_folders",
                return_value={
                    canonical_root(busy): True,
                    canonical_root(newcomer): True,
                },
            ):
                retired, added = rediscovered_roots(states, load_config(), 2, set())

            self.assertEqual(added, [canonical_root(newcomer)])
            self.assertEqual(retired, [canonical_root(idle)])

    def test_liveness_covers_every_watched_repository(self) -> None:
        now = int(time.time() * 1000)
        with sandbox() as directory:
            first = repository(directory)
            second = directory / "second"
            second.mkdir()
            git(second, "init", "-b", "main")
            git(second, "config", "user.email", "side-dog@example.com")
            git(second, "config", "user.name", "Side Dog")
            (second / "README.md").write_text("start\n")
            git(second, "add", "README.md")
            subprocess.run(
                ["git", "commit", "-m", "start"],
                cwd=second,
                check=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00 +0000",
                    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00 +0000",
                },
            )
            git(second, "worktree", "add", os.fspath(directory / "old"), "-b", "old")
            occupied = canonical_root(directory / "old")
            watched = [canonical_root(first), canonical_root(second)]

            # An agent sits in a year-old worktree of the second repository.
            def folders(root: Path) -> set[Path]:
                return {occupied} if root == watched[1] else set()

            with patch("side_dog.cli.agent_folders", side_effect=folders):
                self.assertEqual(busy_worktrees(watched, now, 8), [occupied])

    def test_a_pane_that_is_not_a_coding_agent_gets_no_row(self) -> None:
        with sandbox() as directory:
            root = canonical_root(directory)
            visitor = {
                "agent": "aider",
                "foreground_cwd": os.fspath(root),
                "pane_id": "w1:p9",
                "agent_status": "working",
            }
            coder = {
                "agent": "claude",
                "foreground_cwd": os.fspath(root),
                "pane_id": "w1:p1",
                "agent_status": "working",
            }
            with (
                patch("side_dog.cli.git_worktree_root", return_value=""),
                patch("side_dog.cli.git_common_dir", return_value=""),
                patch("side_dog.cli.load_claude_metadata", return_value={}),
            ):
                identities = herdr_identities_for_root(root, [visitor, coder])

            self.assertEqual(list(identities), ["pane:w1:p1"])

    def test_a_pin_that_arrived_by_itself_is_still_pinned(self) -> None:
        from side_dog.panel import PanelFeed

        with sandbox() as directory:
            pin = directory / "pinned"
            other = directory / "other"
            for folder in (pin, other):
                folder.mkdir()
            config_path().write_text(f'pin = ["{pin}"]\n')
            pinned_root = canonical_root(pin)

            # The pin arrived through Herdr, so it is a root but not requested.
            feed = PanelFeed(
                [pinned_root, canonical_root(other)], requested_roots=[]
            )
            try:
                self.assertIn(pinned_root, feed._pinned)
                self.assertEqual(
                    [state.root for state in feed.roots].count(pinned_root), 1
                )
            finally:
                feed._executor.shutdown(wait=False)


class HeaderContextTest(TestCase):
    def test_the_header_names_the_repository_behind_all(self) -> None:
        common = "/Users/example/src/cocos-story/.git"
        states = []
        for name in ("develop", "issue-9443"):
            state = root_state(Path(f"/tmp/{name}"), [], branch=name)
            state.git_status["common_dir"] = common
            states.append(state)

        self.assertEqual(
            watch_repository_context(states), "/Users/example/src/cocos-story"
        )

    def test_two_repositories_name_the_first_and_count_the_rest(self) -> None:
        first = root_state(Path("/tmp/one"), [], branch="main")
        first.git_status["common_dir"] = "/Users/example/src/one/.git"
        second = root_state(Path("/tmp/two"), [], branch="main")
        second.git_status["common_dir"] = "/Users/example/src/two/.git"

        self.assertEqual(
            watch_repository_context([first, second]), "/Users/example/src/one +1"
        )

    def test_a_folder_without_git_falls_back_to_its_own_path(self) -> None:
        self.assertEqual(
            watch_repository_context([root_state(Path("/tmp/plain"), [])]),
            "/tmp/plain",
        )

    def test_the_rendered_header_carries_repository_and_found(self) -> None:
        screen = render(
            [],
            Path("/tmp/develop"),
            width=100,
            height=10,
            color=False,
            root_count=4,
            repository_context="~/src/cocos-story",
            discovered=True,
        )

        self.assertIn("FOCUS: ALL · ~/src/cocos-story", screen)
        self.assertIn("Watching 4 found folders", screen)

        focused = render(
            [],
            Path("/tmp/develop"),
            width=100,
            height=10,
            color=False,
            root_count=4,
            focused_root_label="PR #9444",
            repository_context="~/src/cocos-story",
            discovered=True,
        )

        self.assertIn("FOCUS: PR #9444 · ~/src/cocos-story", focused)
        self.assertIn("PR #9444 · 1 of 4 found folders", focused)


class ConfiguredLimitReconciliationTest(TestCase):
    def test_a_full_panel_at_the_configured_limit_still_swaps_roots(self) -> None:
        from side_dog.panel import PanelFeed

        with sandbox() as directory:
            config_path().write_text("[display]\nlimit = 2\n")
            quiet_one = directory / "quiet-one"
            quiet_two = directory / "quiet-two"
            newcomer = directory / "newcomer"
            for folder in (quiet_one, quiet_two, newcomer):
                folder.mkdir()
            watched = [canonical_root(quiet_one), canonical_root(quiet_two)]
            live = canonical_root(newcomer)

            feed = PanelFeed(watched, follow_herdr=True, requested_roots=[])
            try:
                with (
                    patch(
                        "side_dog.panel.herdr_session_roots",
                        return_value=([live], None),
                    ),
                    patch("side_dog.panel.busy_worktrees", return_value=[]),
                    patch("side_dog.cli.load_herdr_identities", return_value={}),
                ):
                    feed._follow_worktree_changes(now=1_000_000.0)

                roots = [state.root for state in feed.roots]
                # The reconciliation must know the room is 2, not the built-in
                # 10, or it retires nothing and the newcomer is dropped.
                self.assertIn(live, roots)
                self.assertLessEqual(len(roots), 2)
            finally:
                feed._executor.shutdown(wait=False)
