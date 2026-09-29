from __future__ import annotations

import unittest

from execution_coordinator.portfolio_runtime import read_portfolio_runtime
from tests.test_bootstrap_pickup import NOW, _Reader, _Store, _portfolio_control


class PortfolioRuntimeTests(unittest.TestCase):
    def test_complete_two_repository_read_preserves_rank_and_requirements(self) -> None:
        ca, ta, _ = _portfolio_control("owner/a", 201, 8, priority="P1")
        cb, tb, _ = _portfolio_control("owner/b", 202, 9, priority="P2")
        store = _Store()
        before = store.body

        result = read_portfolio_runtime(
            (ca, cb),
            issue_reader=_Reader(ca, cb, ta, tb),
            state_reader=store,
            worker_id="controller:claude-v1",
            now=NOW,
        )

        self.assertTrue(result.complete)
        self.assertIsNone(result.metadata_error)
        self.assertIsNotNone(result.ranked)
        assert result.ranked is not None
        self.assertEqual(
            ["owner/a#8", "owner/b#9"],
            [item.candidate.task for item in result.ranked.ranked],
        )
        by_task = {item.task: item for item in result.requirements}
        self.assertEqual(frozenset({"python"}), by_task["owner/a#8"].required_capabilities)
        self.assertEqual(frozenset({"windows"}), by_task["owner/a#8"].required_environment)
        self.assertEqual(before, store.body)

    def test_missing_metadata_fails_closed_without_ranked_work(self) -> None:
        ca, ta, _ = _portfolio_control("owner/a", 201, 8)
        cb, tb, _ = _portfolio_control("owner/b", 202, 9, metadata=False)

        result = read_portfolio_runtime(
            (ca, cb),
            issue_reader=_Reader(ca, cb, ta, tb),
            state_reader=_Store(),
            worker_id="controller:claude-v1",
            now=NOW,
        )

        self.assertFalse(result.complete)
        self.assertIsNone(result.ranked)
        self.assertEqual((), result.requirements)
        self.assertIsNotNone(result.metadata_error)

    def test_future_ready_at_is_filtered_by_existing_ranker(self) -> None:
        ca, ta, _ = _portfolio_control(
            "owner/a", 201, 8, ready_at="2026-09-28T16:05:00Z"
        )
        cb, tb, _ = _portfolio_control("owner/b", 202, 9)

        result = read_portfolio_runtime(
            (ca, cb),
            issue_reader=_Reader(ca, cb, ta, tb),
            state_reader=_Store(),
            worker_id="controller:claude-v1",
            now=NOW,
        )

        self.assertTrue(result.complete)
        self.assertIsNotNone(result.ranked)
        assert result.ranked is not None
        self.assertEqual(
            ["owner/b#9"],
            [item.candidate.task for item in result.ranked.ranked],
        )


if __name__ == "__main__":
    unittest.main()
