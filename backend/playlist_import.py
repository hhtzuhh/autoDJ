"""Resolve an uploaded M3U against the configured library; never load its paths."""
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_PLAYLIST_BYTES = 32 * 1024


def preview_playlist(filename: str, content: str, catalog: dict, library_root: Path) -> dict:
    if not isinstance(filename, str) or Path(filename).suffix.lower() not in {".m3u", ".m3u8"}:
        raise ValueError("Choose a UTF-8 .m3u or .m3u8 playlist exported from Mixxx")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_PLAYLIST_BYTES or "\0" in content:
        raise ValueError("Playlist must be UTF-8 text, at most 32 KB")
    entries = [(line, value.strip()) for line, value in enumerate(content.lstrip("\ufeff").splitlines(), 1)
               if value.strip() and not value.lstrip().startswith("#")]
    if not 2 <= len(entries) <= 50:
        raise ValueError("Playlist must contain 2–50 unique local songs")
    paths = {str(track.path.resolve()): track for track in catalog.values()}
    tracks, issues, seen = [], [], set()
    for line, entry in entries:
        path = entry
        if entry.startswith("file:"):
            url = urlsplit(entry)
            if url.netloc not in {"", "localhost"} or url.query or url.fragment:
                issues.append({"line": line, "entry": entry, "reason": "Only local file URLs are supported"})
                continue
            path = unquote(url.path)
        elif "://" in entry or entry.startswith("\\\\"):
            issues.append({"line": line, "entry": entry, "reason": "Remote URLs are not supported"})
            continue
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = library_root / candidate
        track = paths.get(str(candidate.resolve()))
        if track is None:
            issues.append({"line": line, "entry": entry, "reason": "No matching song in local-assets/samples/; absolute paths must match, relative paths start at that folder"})
            continue
        if track.id in seen:
            issues.append({"line": line, "entry": entry, "reason": "Repeated songs are not supported in the current queue"})
            continue
        seen.add(track.id)
        tracks.append(track.public())
    return {"filename": Path(filename).name, "entryCount": len(entries), "tracks": tracks,
            "issues": issues, "canImport": not issues, "missingAnalysis": sum(t["analysisStatus"] != "ready" for t in tracks)}
