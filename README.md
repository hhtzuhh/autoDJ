# autoDJ

## Demo

[Watch the autoDJ demo on YouTube](https://www.youtube.com/watch?v=k-gOUhyPsPI)

Created for the Meta Global AI Developer Hackathon. Music sources and licenses
are listed in Music Credits below.

README rewrite in progress. Historical documentation and earlier library credits
are retained locally and excluded from this repository. Current demo credits are below.

## Current audio library

Place audio in **`local-assets/samples-v3/`** inside this repository. This is the
default for library scanning, relative M3U imports, and prepared Mixxx playlist
exports. The earlier `samples/` and `samples-v2/` folders are retained locally and
are not scanned by default. Audio remains ignored by Git.

Additional tracks can be placed in this same folder without changing code. Update
Music Credits with each added track's own source and license; an artist's license
does not automatically cover other downloads.

Restart `npm run bridge` to refresh the startup library, then Analyze / Build to
prepare the new tracks and a new set. These actions also rescan the current folder.
Analysis caches are keyed by audio hash; an old set is not converted into a new one.
Adding files alone does not start analysis, model calls, or playback.
Any archive setup instructions mentioning `samples/` or `samples-v2/` should now
use `samples-v3/`.

## Music Credits

The following tracks are selected for use in this Autonomous DJ demonstration:

### Dislocated — SampleSwap

- **[Blur](https://sampleswap.org/mp3/song.php?id=3539)** — Dislocated
- **[WBF91](https://sampleswap.org/mp3/song.php?id=3548)** — Dislocated
- **[Alarme](https://sampleswap.org/mp3/song.php?id=3558)** — Dislocated

Source: [Dislocated on SampleSwap](https://sampleswap.org/artist/DiSloCaTed)

License: [Creative Commons Attribution 3.0 Unported (CC BY 3.0)](https://creativecommons.org/licenses/by/3.0/)

Verified on 2026-11-04: the artist listing and the individual pages for these
three tracks label them Attribution and link to CC BY 3.0. Other tracks on the same
artist page have different licenses; this confirmation covers only the three above.

### JMHBM — Free Music Archive

- **[Senseless](https://freemusicarchive.org/music/beat-mekanik/single/senseless/)** — JMHBM. Source: Free Music Archive. License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **[Keepsake](https://freemusicarchive.org/music/beat-mekanik/single/keepsake-1/)** — JMHBM. Source: Free Music Archive. License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **[Only Now](https://freemusicarchive.org/music/beat-mekanik/single/only-now/)** — JMHBM. Source: Free Music Archive. License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Composer/producer: Johnathon M. Horner (Beat Mekanik). The FMA track listings
identify all three licenses as Attribution 4.0 International. Personal and
commercial use, including adaptations, is allowed under the license terms.

For Keepsake, the artist additionally requests credit to producer **@beatmekanik**,
courtesy of Free Music Archive under Creative Commons 4.0 International; on
Facebook, Instagram, or Threads, use **Music by @beat.mekanik**.

### Additional tracks — Free Music Archive

- **[Background](https://freemusicarchive.org/music/1000-handz/cc-by-free-to-use-dancehouse-instrumentals/background-1/)** — 1000 Handz ([1000Handz.com](https://1000handz.com)). Source: Free Music Archive. License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **[AUX ★ Original Mix](https://freemusicarchive.org/music/akira-sora/single/aux-original-mix/)** — Akira Sora. Source: Free Music Archive. License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Both FMA track listings identify Attribution 4.0 International. Personal and
commercial use, including adaptations, is allowed under the license terms.

### Use in this demonstration

The tracks may be modified through automated beatmatching, tempo adjustment,
transitions, and mixing. All music rights remain with their respective copyright
holders. The artists do not endorse this project.
This source/license check does not certify that the tracks contain no vocals.

For publication, include these credits and source/license links in the video
description, retain supplied notices, and identify the modifications actually
used. The list documents the demonstration library; the final edit may use only
excerpts from a subset of these tracks.
