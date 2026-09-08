from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from side_dog.board import LinkedIssue, branch_issue_numbers, issue_cell, detect_conflicts
from side_dog.cli import BoardIssueVerifier, load_board_issue
from tests import test_board


class VerificationTest(TestCase):
    def row(self, repository="github.com/o/r"):
        return test_board.IssueCellTest.row((LinkedIssue(repository, 139, False),))

    def test_one_shot_collects_successful_candidates_in_multiple_batches(self):
        verifier = BoardIssueVerifier()
        executor = Mock()
        def completed(*args):
            future = Future()
            future.set_result(True)
            return future
        executor.submit.side_effect = completed
        rows = [self.row(f"github.com/o/r{i}") for i in range(6)]
        verifier.settle_once(rows, executor, 1)
        self.assertTrue(all(row.issues for row in verifier.refresh(rows, executor, 0)))
        self.assertEqual(executor.submit.call_count, 6)

    def test_one_shot_timeout_leaves_unavailable_candidates_blank(self):
        verifier = BoardIssueVerifier()
        executor = Mock()
        executor.submit.return_value = Future()
        row = self.row()
        verifier.settle_once([row], executor, 0)
        self.assertEqual(verifier.refresh([row], executor, 0)[0].issues, ())

    def test_only_explicit_branch_markers_are_candidates(self):
        for branch in ("claude/launch-test-results-20260907", "build-123", "123-build", "release/2.0.0", "fix/139"):
            with self.subTest(branch=branch):
                self.assertEqual(branch_issue_numbers(branch), ())
        for branch in ("codex/issue-139", "issues/139", "fix/#139-name"):
            self.assertEqual(branch_issue_numbers(branch), (139,))

    def test_pending_lookup_does_not_block_and_success_is_cached_until_expiry(self):
        verifier = BoardIssueVerifier()
        future = Future()
        executor = Mock()
        executor.submit.return_value = future
        row = self.row()
        self.assertEqual(verifier.refresh([row, row], executor, 0)[0].issues, ())
        executor.submit.assert_called_once_with(load_board_issue, "github.com/o/r", 139)
        future.set_result(True)
        self.assertEqual(issue_cell(verifier.refresh([row], executor, 1)[0]), "#139")
        self.assertEqual(issue_cell(verifier.refresh([row], executor, 300)[0]), "#139")
        executor.submit.return_value = Future()
        self.assertEqual(verifier.refresh([row], executor, 301)[0].issues, ())
        self.assertEqual(executor.submit.call_count, 2)

    def test_failure_is_cached_and_repository_isolation_is_preserved(self):
        verifier = BoardIssueVerifier()
        executor = Mock()
        failed, other = Future(), Future()
        executor.submit.side_effect = [failed, other]
        row = self.row()
        verifier.refresh([row], executor, 0)
        failed.set_exception(OSError("offline"))
        self.assertEqual(verifier.refresh([row], executor, 1)[0].issues, ())
        verifier.refresh([row, self.row("github.com/other/r")], executor, 2)
        self.assertEqual(executor.submit.call_count, 2)

    def test_confirmed_evidence_needs_no_lookup_and_candidates_never_conflict(self):
        row = self.row()
        other = replace(row, key="codex:other", working_root="/other", branch="other")
        self.assertEqual(detect_conflicts([row, other]), [])
        confirmed = replace(row, issues=(row.issues[0]._replace(confirmed=True),))
        executor = Mock()
        self.assertEqual(BoardIssueVerifier().refresh([confirmed], executor, 0), [confirmed])
        executor.submit.assert_not_called()

    def test_cache_and_queue_are_bounded(self):
        verifier = BoardIssueVerifier()
        verifier.limit = 2
        executor = Mock(side_effect=None)
        executor.submit.side_effect = lambda *args: Future()
        verifier.refresh([self.row(f"github.com/o/r{i}") for i in range(10)], executor, 0)
        self.assertEqual(len(verifier.pending), 2)

    @patch("side_dog.cli.subprocess.run")
    def test_lookup_validates_number_repository_and_issue_url(self, run):
        for body, expected in (
            ('{"number":139,"url":"https://github.com/o/r/issues/139"}', True),
            ('{"number":139,"url":"https://github.com/other/r/issues/139"}', False),
            ('{"number":139,"url":"https://github.com/o/r/pull/139"}', False),
            ('{"number":140,"url":"https://github.com/o/r/issues/140"}', False),
            ('{}', False), ("bad json", False),
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout=body)
            self.assertEqual(load_board_issue("github.com/o/r", 139), expected)
        run.return_value = SimpleNamespace(returncode=1, stdout="")
        self.assertFalse(load_board_issue("github.com/o/r", 139))
