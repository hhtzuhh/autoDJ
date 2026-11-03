import unittest

from allin1_normalize import normalize_beats


class NormalizeBeatsTests(unittest.TestCase):
    def test_fills_half_tempo_gap_and_rebuilds_positions(self):
        result = {
            "bpm": 120,
            "beats": [0.0, 0.5, 1.5, 2.0],
            "beat_positions": [1, 2, 3, 4],
            "downbeats": [0.0],
            "segments": [],
        }
        normalized, diagnostics = normalize_beats(result)
        self.assertEqual(normalized["beats"], [0.0, 0.5, 1.0, 1.5, 2.0])
        self.assertEqual(normalized["beat_positions"], [1, 2, 3, 4, 1])
        self.assertEqual(normalized["downbeats"], [0.0, 2.0])
        self.assertEqual(diagnostics["inserted_beat_count"], 1)
        self.assertTrue(diagnostics["preserves_original_beat_timestamps"])

    def test_does_not_fill_an_untracked_long_region(self):
        result = {
            "bpm": 120,
            "beats": [0.0, 3.0, 3.5],
            "beat_positions": [1, 2, 3],
            "downbeats": [0.0],
            "segments": [],
        }
        normalized, diagnostics = normalize_beats(result)
        self.assertEqual(normalized["beats"], [0.0, 3.0, 3.5])
        self.assertEqual(diagnostics["inserted_beat_count"], 0)


if __name__ == "__main__":
    unittest.main()
