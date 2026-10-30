from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from setlist_service import resolve_set_list, write_mixxx_playlist
from transition_pipeline import AnalysisCatalog, AnalysisTrack


class SetListServiceTest(unittest.TestCase):
    def test_resolves_hash_verified_queue_and_writes_ordered_playlist(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "library"
            library.mkdir()
            files = []
            tracks = {}
            for index, content in enumerate((b"first", b"second"), start=1):
                source = library / f"track-{index}.wav"
                source.write_bytes(content)
                track_id = hashlib.sha256(content).hexdigest()
                files.append(source)
                tracks[track_id] = AnalysisTrack(
                    track_id=track_id,
                    analysis_cache_key=f"key-{index}",
                    source_filename=source.name,
                    analysis_path=root / f"analysis-{index}.json",
                )
            queue = {
                "revision": 4,
                "ordered_track_ids": list(reversed(tuple(tracks))),
            }

            manifest = resolve_set_list(AnalysisCatalog(tracks), queue, [library])
            playlist = root / "tonight.m3u8"
            manifest_path = root / "tonight.json"
            write_mixxx_playlist(manifest, playlist, manifest_path)

            expected_paths = [str(files[1].resolve()), str(files[0].resolve())]
            self.assertEqual(
                playlist.read_text(encoding="utf-8").splitlines(),
                ["#EXTM3U", *expected_paths],
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["queue_revision"], 4)
            self.assertEqual(
                [row["track_id"] for row in payload["ordered_tracks"]],
                queue["ordered_track_ids"],
            )

    def test_rejects_file_that_does_not_match_analysis_identity(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "track.wav"
            source.write_bytes(b"wrong")
            expected_id = hashlib.sha256(b"expected").hexdigest()
            catalog = AnalysisCatalog(
                {
                    expected_id: AnalysisTrack(
                        expected_id,
                        "key",
                        source.name,
                        root / "analysis.json",
                    )
                }
            )
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                resolve_set_list(
                    catalog,
                    {"revision": 1, "ordered_track_ids": [expected_id]},
                    [root],
                )


if __name__ == "__main__":
    unittest.main()
