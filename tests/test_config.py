from __future__ import annotations

import io
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    STATE_ENV,
    WATCH_ROOT_LIMIT,
    display_settings_path,
    load_display_settings,
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
    read_toml,
)


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
            {CONFIG_HOME_ENV: os.fspath(home), STATE_ENV: directory + "/state"},
        ):
            if config is not None:
                config_path().parent.mkdir(parents=True, exist_ok=True)
                config_path().write_text(config)
            yield Path(directory)


def render_once(root: Path) -> str:
    stream = TtyStream()
    with (
        patch("side_dog.cli.load_herdr_identities", return_value={}),
        patch("side_dog.cli.sys.stdout", stream),
        patch("side_dog.cli.sys.stdin", stream),
    ):
        watch(
            os.fspath(root),
            width=80,
            poll=0.0,
            no_color=True,
            github_poll=0.0,
            once=True,
        )
    return stream.getvalue()


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
