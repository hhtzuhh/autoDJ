# Private local inputs

Put current library audio in `samples-v3/`. The previous `samples/` and `samples-v2/` folders are
retained locally and are no longer scanned by default. See the main
[README](../README.md#music-credits) for track sources and CC BY credits.

Files here are ignored by Git; only these
directory instructions belong in the public repository. Do not force-add songs,
footage, credentials or other private inputs. Nothing here is served as a Vite
public asset. The development bridge scans the audio library when it starts.
