from pathlib import Path
import tempfile
import unittest

from lifecycle_supervisor import LifecycleSupervisor
from plan_lifecycle import (
    DeckPerformance,
    PerformanceSnapshot,
    PlanEnvelope,
    PlanLifecycleManager,
)
from queue_service import QueueSnapshot, QueueStore


A_ID = "a" * 64
B_ID = "b" * 64
A_KEY = "1" * 64
B_KEY = "2" * 64


def selection() -> dict:
    return {
        "source_window_id": "window-test",
        "queue_revision": 4,
        "next_executable_plan": {
            "plan_id": "plan-test",
            "deck_a": {
                "audio_sha256": A_ID,
                "analysis_cache_key": A_KEY,
                "deck": 1,
                "play_start_beat_index": 92,
                "transition_start_beat_index": 100,
            },
            "deck_b": {
                "audio_sha256": B_ID,
                "analysis_cache_key": B_KEY,
                "deck": 2,
                "cue_beat_index": 40,
            },
        },
    }


def performance(
    *,
    a_beat: int | None = 80,
    a_play: bool | None = True,
    b_id: str = B_ID,
) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        revision=8,
        decks={
            1: DeckPerformance(1, A_ID, A_KEY, a_beat, a_play),
            2: DeckPerformance(2, b_id, B_KEY, 40, False),
        },
    )


class LifecycleSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.queue_path = root / "queue.json"
        self.store = QueueStore(self.queue_path)
        self.store.save(
            QueueSnapshot(
                revision=4,
                ordered_track_ids=(A_ID, B_ID, "c" * 64),
                updated_at="2026-10-29T00:00:00+00:00",
            )
        )
        self.supervisor = LifecycleSupervisor(
            selection=selection(),
            queue_store=self.store,
            envelope_path=root / "envelope.json",
            executor_plan_path=root / "executor-plan.json",
            launch_executor=False,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ready_preconditions(self) -> None:
        self.assertIsNone(self.supervisor._wait_reason(performance()))

    def test_waits_for_exact_tracks(self) -> None:
        self.assertEqual(
            self.supervisor._wait_reason(performance(b_id="x" * 64)),
            "expected_tracks_not_loaded",
        )

    def test_waits_for_source_position_and_transport(self) -> None:
        self.assertEqual(
            self.supervisor._wait_reason(performance(a_beat=None)),
            "source_beat_unknown",
        )
        self.assertEqual(
            self.supervisor._wait_reason(performance(a_beat=93)),
            "latest_arm_beat_passed",
        )
        self.assertEqual(
            self.supervisor._wait_reason(performance(a_play=False)),
            "source_deck_not_playing",
        )

    def test_waits_for_target_deck_to_stop(self) -> None:
        live = performance()
        target = live.decks[2]
        live = PerformanceSnapshot(
            revision=live.revision,
            decks={
                **live.decks,
                2: DeckPerformance(
                    target.deck,
                    target.track_id,
                    target.analysis_cache_key,
                    target.beat_index,
                    True,
                ),
            },
        )

        self.assertEqual(
            self.supervisor._wait_reason(live), "target_deck_is_playing"
        )

    def test_waits_for_bound_queue_revision(self) -> None:
        self.store.save(
            QueueSnapshot(
                revision=5,
                ordered_track_ids=(A_ID, B_ID),
                updated_at="2026-10-29T00:01:00+00:00",
            )
        )
        self.assertEqual(
            self.supervisor._wait_reason(performance()),
            "queue_revision_mismatch",
        )

    def test_dashboard_takeover_cancels_only_active_executor(self) -> None:
        class FakeScheduler:
            def __init__(self) -> None:
                self.cancellations = []

            def cancel_future(self, plan_id, *, preserve_playback):
                self.cancellations.append((plan_id, preserve_playback))

        class RunningExecutor:
            def poll(self):
                return None

        live = performance()
        scheduler = FakeScheduler()
        manager = PlanLifecycleManager(
            PlanEnvelope.from_rolling_selection(selection(), live),
            scheduler,
        )
        manager.mark_ready(live, self.store.load())
        manager.arm(live, self.store.load())
        manager.start_execution(live, self.store.load())
        self.supervisor.manager = manager
        self.supervisor.executor = RunningExecutor()

        accepted, reason = self.supervisor.request_takeover()

        self.assertTrue(accepted)
        self.assertEqual(reason, "human_dashboard_takeover")
        self.assertEqual(manager.envelope.status, "cancelled")
        self.assertEqual(
            scheduler.cancellations,
            [("plan-test", True)],
        )
        self.assertTrue(self.supervisor.envelope_path.exists())

        accepted_again, reason_again = self.supervisor.request_takeover()
        self.assertFalse(accepted_again)
        self.assertEqual(reason_again, "plan_status_cancelled")

    def test_dashboard_takeover_rejects_without_owned_plan(self) -> None:
        accepted, reason = self.supervisor.request_takeover()

        self.assertFalse(accepted)
        self.assertEqual(reason, "no_dashboard_owned_plan")


if __name__ == "__main__":
    unittest.main()
