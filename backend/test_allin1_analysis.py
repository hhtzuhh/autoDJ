import unittest

from allin1_analysis import (
    energy_direction,
    energy_state,
    enrich_candidates,
    reviewed_seconds_to_beats,
    validate_output,
    validate_normalized_rhythm,
)


class UnifiedAnalysisTests(unittest.TestCase):
    def test_energy_direction_and_state_are_separate(self):
        self.assertEqual(
            energy_direction({"before_energy": 0.9, "after_energy": 0.1}),
            "falling",
        )
        self.assertEqual(
            energy_direction({"before_energy": 0.8, "after_energy": 0.85}),
            "stable",
        )
        self.assertEqual(energy_state(0.1), "low")
        self.assertEqual(energy_state(0.5), "medium")
        self.assertEqual(energy_state(0.9), "high")

    def test_structure_label_is_weak_and_not_scored(self):
        candidates = {
            "entry": [
                {
                    "time_seconds": 10.2,
                    "before_energy": 0.8,
                    "after_energy": 0.9,
                }
            ],
            "exit": [],
        }
        result = enrich_candidates(
            candidates,
            [{"start": 10.0, "end": 20.0, "label": "chorus"}],
        )
        entry = result["entry"][0]
        self.assertEqual(entry["boundary_direction"], "stable")
        self.assertEqual(entry["post_cue_energy_state"], "high")
        self.assertEqual(entry["section_boundary"]["weak_label_after"], "chorus")
        self.assertFalse(entry["section_boundary"]["used_in_candidate_score"])

    def test_normalized_grid_must_cycle_in_four_four(self):
        valid = {
            "beats": [0.0, 0.5, 1.0, 1.5],
            "beat_positions": [1, 2, 3, 4],
        }
        validate_normalized_rhythm(valid, 2.0)
        invalid = {**valid, "beat_positions": [1, 2, 4, 1]}
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_normalized_rhythm(invalid, 2.0)

    def test_reviewed_cue_snaps_to_normalized_downbeat(self):
        bars = [
            {"beat_index": 0, "start_seconds": 1.0},
            {"beat_index": 4, "start_seconds": 3.0},
        ]
        self.assertEqual(reviewed_seconds_to_beats(bars, [3.05]), [4])
        with self.assertRaisesRegex(ValueError, "not near"):
            reviewed_seconds_to_beats(bars, [8.0])

    def test_output_validator_rejects_incomplete_schema(self):
        with self.assertRaisesRegex(ValueError, "missing fields"):
            validate_output({"schema_version": "dj-track-analysis-v1"})


if __name__ == "__main__":
    unittest.main()
