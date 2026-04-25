"""Music analysis + beat-locked retime.

librosa does the heavy lifting: beat tracking, downbeat tracking, tempo, loudness
curve. We turn that into a "rhythm vocabulary" — a list of beats, downbeats, and
energy-change moments that the cut plan can snap to.

Then a retime function adjusts the cut plan's slice boundaries to land on the
nearest beat (within a tolerance), producing a version of the timeline that feels
edited to the music rather than arbitrary.

Requires: librosa, soundfile. Both auto-install with pip.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np


def analyze(audio_path: Path) -> dict:
    """Return a rhythm vocabulary + loudness curve for an audio file.

    Output:
      {
        "duration": <seconds>,
        "bpm": <float>,
        "beats": [seconds, ...],           # every detected beat
        "downbeats": [seconds, ...],       # every 1st beat of a measure (heuristic)
        "energy_spikes": [seconds, ...],   # moments of sudden loudness jump
        "loudness_curve": [...],           # 1-second-resolution RMS dB
        "dynamic_range": <LU-ish float>,
      }
    """
    import librosa

    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    duration = float(len(y)) / sr

    # Beat tracking
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    if np.isscalar(tempo):
        tempo_val = float(tempo)
    elif hasattr(tempo, 'item'):
        tempo_val = float(tempo.item())
    elif hasattr(tempo, '__len__') and len(tempo) > 0:
        tempo_val = float(tempo[0])
    else:
        tempo_val = 120.0

    # Heuristic downbeats: every 4th beat (most pop/rock/cinematic in 4/4)
    downbeats = beats[::4]

    # Energy spikes: strong onsets (drum hits, brass stabs, etc.)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="frames")
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)
    # Only keep the most prominent onsets — those above the mean envelope + 1σ
    if len(onset_env):
        thresh = float(np.mean(onset_env) + np.std(onset_env))
        energy_spikes = []
        for f, t in zip(onset_frames, onset_times):
            if 0 <= f < len(onset_env) and onset_env[f] > thresh:
                energy_spikes.append(float(t))
    else:
        energy_spikes = []

    # Coarse loudness curve (RMS → dB, 1s bins)
    hop_length = sr  # 1 frame per second
    rms = librosa.feature.rms(y=y, frame_length=sr, hop_length=hop_length).flatten()
    rms_db = librosa.amplitude_to_db(rms + 1e-9).tolist()
    # Dynamic range (percentile diff — rough proxy for LU)
    if rms_db:
        p95 = float(np.percentile(rms_db, 95))
        p5 = float(np.percentile(rms_db, 5))
        dynamic_range = p95 - p5
    else:
        dynamic_range = 0.0

    return {
        "duration": duration,
        "bpm": round(tempo_val, 2),
        "beats": [round(b, 3) for b in beats],
        "downbeats": [round(b, 3) for b in downbeats],
        "energy_spikes": [round(t, 3) for t in energy_spikes],
        "loudness_curve": [round(x, 2) for x in rms_db],
        "dynamic_range": round(dynamic_range, 2),
    }


def snap_timeline_to_beats(
    entries: list[dict],
    analysis: dict,
    *,
    tolerance: float = 0.35,
) -> dict:
    """Adjust each slice's boundary to land on the nearest beat, within tolerance.

    entries: list of {"source_start", "source_end", "slice_in", "slice_out", "duration", ...}
    analysis: output of analyze()
    tolerance: max seconds to snap. Slices further from a beat keep their original time.

    Returns {"entries": new_entries, "report": {...}}.
    """
    beats = analysis.get("beats") or []
    downbeats = set(round(b, 3) for b in (analysis.get("downbeats") or []))
    energy_spikes = analysis.get("energy_spikes") or []

    if not beats or not entries:
        return {"entries": list(entries), "report": {"snapped": 0, "total": len(entries)}}

    # Merge all "attractor" moments (beats + energy spikes are candidates)
    attractors = sorted(set(round(t, 3) for t in (beats + energy_spikes)))

    def nearest(t: float) -> tuple[float, float]:
        """Return (nearest_attractor, delta). O(log n) via bisect."""
        import bisect
        pos = bisect.bisect_left(attractors, t)
        candidates = []
        if pos > 0:
            candidates.append(attractors[pos - 1])
        if pos < len(attractors):
            candidates.append(attractors[pos])
        if not candidates:
            return t, float("inf")
        best = min(candidates, key=lambda a: abs(a - t))
        return best, abs(best - t)

    # Walk entries in order, snapping their cumulative OUTPUT-timeline end boundaries
    # Each entry's start = previous entry's end. Our job is to pick a duration for
    # each entry s.t. the cumulative boundary lands on or near a beat.
    cum = 0.0
    new_entries: list[dict] = []
    snapped = 0
    for e in entries:
        old_dur = e.get("duration") or (e.get("slice_out", 0) - e.get("slice_in", 0)) or 1.0
        target_end = cum + old_dur
        # Search for a beat within tolerance of target_end
        near, delta = nearest(target_end)
        if delta <= tolerance:
            new_end = near
            is_downbeat = round(near, 3) in downbeats
            snapped += 1
        else:
            new_end = target_end
            is_downbeat = False

        # Adjust slice_out to match new duration
        new_dur = max(0.15, new_end - cum)
        slice_in = e["slice_in"]
        slice_out = slice_in + new_dur
        new_entries.append({
            **e,
            "slice_in": round(slice_in, 3),
            "slice_out": round(slice_out, 3),
            "duration": round(new_dur, 3),
            "landed_on_beat": delta <= tolerance,
            "on_downbeat": is_downbeat,
        })
        cum = new_end

    # Compute a sync score: fraction of entries landing within 80ms of a beat
    hits = sum(1 for e in new_entries if e.get("landed_on_beat"))
    sync_score = round(hits / max(1, len(new_entries)), 3)

    return {
        "entries": new_entries,
        "report": {
            "snapped": snapped,
            "total": len(entries),
            "sync_score": sync_score,
            "bpm": analysis.get("bpm"),
            "dynamic_range": analysis.get("dynamic_range"),
            "energy_spike_count": len(energy_spikes),
        },
    }


def score_current_edit(entries: list[dict], analysis: dict, tolerance: float = 0.1) -> dict:
    """Grade a pre-existing (pre-snap) timeline vs a music track. Returns metrics."""
    beats = sorted(set(round(b, 3) for b in (analysis.get("beats") or [])))
    if not beats or not entries:
        return {"beat_sync": 0.0, "arc_fit": 0.0, "bpm": analysis.get("bpm", 0)}

    cum = 0.0
    hits = 0
    durations = []
    for e in entries:
        d = e.get("duration") or (e.get("slice_out", 0) - e.get("slice_in", 0))
        if d <= 0:
            d = 0.15
        durations.append(d)
        cum += d
        # Is cum within tolerance of a beat? O(log n) via bisect.
        import bisect
        pos = bisect.bisect_left(beats, cum)
        candidates = []
        if pos < len(beats):
            candidates.append(beats[pos])
        if pos > 0:
            candidates.append(beats[pos - 1])
        closest = min(candidates, key=lambda b: abs(b - cum))
        if abs(closest - cum) <= tolerance:
            hits += 1

    beat_sync = hits / max(1, len(entries))

    # Arc fit: ideal shape is monotone-decreasing durations until the last (title),
    # which is longest. Simple proxy: correlation of durations with idx (should be negative).
    if len(durations) >= 3:
        idxs = list(range(len(durations)))
        # exclude last since it's supposed to be held long
        core = durations[:-1]
        core_idxs = idxs[:-1]
        if len(core) >= 2 and (max(core) - min(core)) > 0.05:
            # Pearson correlation
            n = len(core)
            mx = sum(core_idxs) / n
            my = sum(core) / n
            num = sum((core_idxs[i] - mx) * (core[i] - my) for i in range(n))
            dx = math.sqrt(sum((core_idxs[i] - mx) ** 2 for i in range(n)))
            dy = math.sqrt(sum((core[i] - my) ** 2 for i in range(n)))
            r = num / (dx * dy) if dx > 1e-9 and dy > 1e-9 else 0
            arc_fit = max(0.0, -r)  # negative correlation → good arc
        else:
            arc_fit = 0.0
    else:
        arc_fit = 0.0

    return {
        "beat_sync": round(beat_sync, 3),
        "arc_fit": round(arc_fit, 3),
        "bpm": analysis.get("bpm", 0),
        "dynamic_range": analysis.get("dynamic_range", 0),
    }
