# Current demo library (v3)

Place the current demo songs here. The bridge scans this folder; relative M3U
entries and prepared Mixxx playlist exports resolve against it.

Current credits are in the [main README](../../README.md#music-credits):

- Dislocated — Blur, WBF91, Alarme (SampleSwap, CC BY 3.0).
- JMHBM — Senseless, Keepsake, Only Now (Free Music Archive, CC BY 4.0).
- 1000 Handz — Background (Free Music Archive, CC BY 4.0).
- Akira Sora — AUX ★ Original Mix (Free Music Archive, CC BY 4.0).

Additional tracks can be added to this folder without changing code;
record their own source and license in the main README.

Restart the bridge to refresh the library shown at startup, then Analyze / Build
to prepare newly added audio. Those actions also rescan this folder. No analysis
or model request is automatically started simply by adding a file.

Only folder documentation is tracked. Audio remains ignored by Git.
