import json
from pathlib import Path
import tempfile
import unittest

from plan_lifecycle import (
    DeckPerformance,
    FileAutomationScheduler,
    PerformanceSnapshot,
    PlanEnvelope,
    PlanLifecycleManager,
)
from queue_service import QueueSnapshot


A_ID = "a" * 64
B_ID = "b" * 64
A_KEY = "1" * 64
B_KEY = "2" * 64


def selection(queue_revision: int = 4) -> dict:
    return {
        "source_window_id": "window-test",
        "queue_revision": queue_revision,
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
    revision: int = 8,
    *,
    a_beat: int = 80,
    a_id: str = A_ID,
    b_id: str = B_ID,
) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        revision=revision,
        decks={
            1: DeckPerformance(1, a_id, A_KEY, a_beat, True),
            2: DeckPerformance(2, b_id, B_KEY, 40, False),
        },
    )


def queue(revision: int = 4) -> QueueSnapshot:
    return QueueSnapshot(
        revision=revision,
        ordered_track_ids=(A_ID, B_ID, "c" * 64),
        updated_at="2026-10-29T00:00:00+00:00",
    )


class FakeScheduler:
    def __init__(self) -> None:
        self.cancellations = []

    def cancel_future(self, plan_id: str, *, preserve_playback: bool) -> None:
        self.cancellations.append((plan_id, preserve_playback))


class PlanLifecycleTest(unittest.TestCase):
    def manager(self) -> tuple[PlanLifecycleManager, FakeScheduler]:
        initial = performance()
        scheduler = FakeScheduler()
        envelope = PlanEnvelope.from_rolling_selection(selection(), initial)
        return PlanLifecycleManager(envelope, scheduler), scheduler

    def ready_and_arm(self) -> tuple[PlanLifecycleManager, FakeScheduler]:
        manager, scheduler = self.manager()
        manager.mark_ready(performance(), queue())
        manager.arm(performance(), queue())
        return manager, scheduler

    def test_happy_lifecycle(self) -> None:
        manager, scheduler = self.ready_and_arm()

        manager.start_execution(performance(), queue())
        manager.handle_observer_event(
            {"revision": 9, "origin": "automation", "event": "transport_changed"}
        )
        manager.complete()

        self.assertEqual(manager.envelope.status, "completed")
        self.assertEqual(scheduler.cancellations, [])

    def test_tempo_ramp_is_the_start_of_automation_for_human_takeover(self) -> None:
        selected = selection()
        plan = selected["next_executable_plan"]
        plan["automation_start_beat_index"] = 90
        plan["deck_a"]["play_start_beat_index"] = 82
        scheduler = FakeScheduler()
        envelope = PlanEnvelope.from_rolling_selection(selected, performance())
        manager = PlanLifecycleManager(envelope, scheduler)
        manager.mark_ready(performance(), queue())
        manager.arm(performance(), queue())
        manager.handle_observer_event({
            "revision": 9, "origin": "human_or_external", "event": "position_jump",
            "details": {"deck": 1, "beat_index": 92},
        })
        self.assertEqual(envelope.transition_start_beat_index, 90)
        self.assertEqual(envelope.status, "invalidated")
        self.assertEqual(scheduler.cancellations, [("plan-test", True)])

    def test_seek_before_transition_preserves_armed_plan(self) -> None:
        manager, scheduler = self.ready_and_arm()

        manager.handle_observer_event(
            {
                "revision": 9,
                "origin": "human_or_external",
                "event": "position_jump",
                "details": {"deck": 1, "beat_index": 96},
            }
        )

        self.assertEqual(manager.envelope.status, "armed")
        self.assertIsNone(manager.envelope.invalidation_reason)
        self.assertEqual(scheduler.cancellations, [])

    def test_seek_at_transition_invalidates_armed_plan(self) -> None:
        manager, scheduler = self.ready_and_arm()
        manager.handle_observer_event(
            {
                "revision": 9,
                "origin": "human_or_external",
                "event": "position_jump",
                "details": {"deck": 1, "beat_index": 100},
            }
        )
        self.assertEqual(manager.envelope.status, "invalidated")
        self.assertEqual(manager.envelope.invalidation_reason, "human_seek")
        self.assertEqual(scheduler.cancellations, [("plan-test", True)])

    def test_mixer_takeover_cancels_execution_and_preserves_playback(self) -> None:
        manager, scheduler = self.ready_and_arm()
        manager.start_execution(performance(), queue())

        manager.handle_observer_event(
            {
                "revision": 9,
                "origin": "human_or_external",
                "event": "mixer_gesture_started",
            }
        )

        self.assertEqual(manager.envelope.status, "cancelled")
        self.assertEqual(
            manager.envelope.invalidation_reason, "human_mixer_takeover"
        )
        self.assertEqual(scheduler.cancellations, [("plan-test", True)])

    def test_channel_shaping_before_transition_does_not_cancel(self) -> None:
        for control in ("volume", "eq_low", "filter"):
            with self.subTest(control=control):
                manager, scheduler = self.ready_and_arm()
                manager.start_execution(performance(), queue())
                manager.handle_observer_event(
                    {
                        "revision": 9,
                        "origin": "human_or_external",
                        "event": "channel_control_changed",
                        "source_beat_index": 96,
                        "details": {"deck": 1, "control": control, "value": 0.6},
                    }
                )

                self.assertEqual(manager.envelope.status, "executing")
                self.assertEqual(manager.envelope.last_performance_revision, 9)
                self.assertEqual(scheduler.cancellations, [])

    def test_channel_shaping_at_transition_is_takeover(self) -> None:
        manager, scheduler = self.ready_and_arm()
        manager.start_execution(performance(), queue())

        manager.handle_observer_event(
            {
                "revision": 9,
                "origin": "human_or_external",
                "event": "channel_control_changed",
                "source_beat_index": 100,
                "details": {"deck": 1, "control": "eq_low", "value": 0.6},
            }
        )

        self.assertEqual(manager.envelope.status, "cancelled")
        self.assertEqual(
            manager.envelope.invalidation_reason, "human_channel_takeover"
        )
        self.assertEqual(scheduler.cancellations, [("plan-test", True)])

    def test_crossfader_before_transition_remains_takeover(self) -> None:
        manager, scheduler = self.ready_and_arm()
        manager.start_execution(performance(), queue())

        manager.handle_observer_event(
            {
                "revision": 9,
                "origin": "human_or_external",
                "event": "mixer_gesture_started",
                "source_beat_index": 96,
                "details": {"control": "crossfader", "value": 0.6},
            }
        )

        self.assertEqual(manager.envelope.status, "cancelled")
        self.assertEqual(scheduler.cancellations, [("plan-test", True)])

    def test_loop_and_track_change_invalidate(self) -> None:
        for event_name, expected in (
            ("loop_changed", "human_loop"),
            ("track_identity_changed", "human_track_change"),
        ):
            with self.subTest(event=event_name):
                manager, _ = self.ready_and_arm()
                manager.handle_observer_event(
                    {
                        "revision": 9,
                        "origin": "human_or_external",
                        "event": event_name,
                    }
                )
                self.assertEqual(manager.envelope.invalidation_reason, expected)

    def test_queue_revision_change_invalidates(self) -> None:
        manager, scheduler = self.ready_and_arm()

        manager.handle_queue(queue(5))

        self.assertEqual(manager.envelope.status, "invalidated")
        self.assertEqual(
            manager.envelope.invalidation_reason, "queue_revision_changed"
        )
        self.assertEqual(scheduler.cancellations, [("plan-test", True)])

    def test_track_identity_mismatch_prevents_ready(self) -> None:
        manager, _ = self.manager()

        with self.assertRaisesRegex(ValueError, "track_identity_mismatch"):
            manager.mark_ready(performance(b_id="x" * 64), queue())

        self.assertEqual(manager.envelope.status, "draft")

    def test_latest_arm_beat_expiry_invalidates(self) -> None:
        manager, _ = self.manager()

        manager.reconcile_performance(performance(a_beat=93))

        self.assertEqual(manager.envelope.status, "invalidated")
        self.assertEqual(
            manager.envelope.invalidation_reason, "latest_arm_beat_passed"
        )

    def test_unattributed_revision_change_fails_closed(self) -> None:
        manager, _ = self.manager()
        manager.mark_ready(performance(), queue())

        with self.assertRaisesRegex(ValueError, "performance_revision"):
            manager.arm(performance(revision=9), queue())

        self.assertEqual(manager.envelope.status, "invalidated")

    def test_file_scheduler_writes_plan_specific_preserve_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "takeover.json"
            scheduler = FileAutomationScheduler(path)

            scheduler.cancel_future("plan-test", preserve_playback=True)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["plan_id"], "plan-test")
            self.assertTrue(payload["preserve_playback"])
            with self.assertRaisesRegex(ValueError, "preserve playback"):
                scheduler.cancel_future("plan-test", preserve_playback=False)


if __name__ == "__main__":
    unittest.main()
