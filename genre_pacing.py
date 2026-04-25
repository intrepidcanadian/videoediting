"""Genre-specific pacing templates — trailer grammar varies by genre.

A horror trailer's rhythm is nothing like a comedy's. Our storyboard writer used
to treat all trailers identically. This module exposes genre templates that get
injected into the system prompt when the user picks a genre.

Each genre has:
  - pacing: cadence rule (shot-duration shape, cut frequency, held beats)
  - camera: signature camera behavior
  - cutting_rules: where to place cuts
  - vocabulary: DO words
  - antipatterns: DON'T words
  - typical_durations: preferred shot-duration range (informs Claude's duration picks)

These templates are intentionally opinionated — they encode genre grammar that
real editors follow. Users can override any shot's duration post-storyboard; this
is just the starting point.
"""

from __future__ import annotations

GENRES = {
    "neutral": {
        "label": "Neutral (no genre bias)",
        "pacing": "Default cinematic pacing. 3-6s shots, escalate toward climax.",
        "camera": "Varied — use what the beat requires.",
        "cutting_rules": "Cut on motion or beat change.",
        "vocabulary": [],
        "antipatterns": [],
        "typical_durations": [3, 7],
    },
    "horror": {
        "label": "Horror / supernatural thriller",
        "pacing": (
            "Long quiet holds (4-6s) on wide environmental shots — let the audience scan for what's wrong. "
            "Then sudden cuts to close-ups at the peak (1-2s). Silence is a weapon. "
            "One extended held beat before the title card."
        ),
        "camera": (
            "Mostly handheld or locked-off wides. Slow zooms on stillness. Never smooth track on anything that could "
            "be menacing. Sudden whip-ins for shock moments only."
        ),
        "cutting_rules": (
            "Cut on gaze (eyes opening, looking offscreen), on darkness filling frame, on sudden sound triggers. "
            "Pre-title hold should last uncomfortably long."
        ),
        "vocabulary": ["dread", "unsettle", "menace", "conceal", "loom", "creep", "lingering"],
        "antipatterns": ["bright daylight", "fast cutting throughout", "comedic beats", "hero music swells"],
        "typical_durations": [2, 7],
    },
    "action": {
        "label": "Action / thriller",
        "pacing": (
            "Escalating montage. Opens with a 3-4s tension hook, builds through 2-3s mid-shots, climaxes with "
            "0.7-1.5s rapid cuts on peak beats. Held title afterwards (3s) to let the audience breathe."
        ),
        "camera": (
            "Dolly + handheld + whip pans. Motion on every shot. Tracking shots alongside or behind a running subject. "
            "Slo-mo peaks at emotional beats, then snap back to real-time."
        ),
        "cutting_rules": (
            "Cut on motion — impact, hit, jump, gunshot. Cut on the frame BEFORE the impact lands for maximum punch. "
            "Never two stationary shots in a row."
        ),
        "vocabulary": ["velocity", "impact", "confrontation", "pursuit", "collision", "urgent", "kinetic"],
        "antipatterns": ["static subject without motion", "wide contemplative holds", "melancholy palette"],
        "typical_durations": [1, 5],
    },
    "drama": {
        "label": "Drama / character study",
        "pacing": (
            "Slow, deliberate. 5-8s shots. Held performance beats. Delayed reveals. Let faces play. "
            "Build emotional weight through sustained composition rather than cuts."
        ),
        "camera": (
            "Locked-off frames. Slow dolly-in on faces. Handheld only for intimacy, not chaos. "
            "Soft focus transitions, rack focus to pull attention."
        ),
        "cutting_rules": (
            "Cut on emotional landing, not physical action. Give performances time to breathe. "
            "Match cuts on eyelines. No cuts just to move forward — every cut earns its place."
        ),
        "vocabulary": ["reckoning", "quiet", "weight", "inheritance", "fracture", "ritual", "unspoken"],
        "antipatterns": ["fast cuts", "action vocabulary", "bombastic music cues"],
        "typical_durations": [5, 10],
    },
    "comedy": {
        "label": "Comedy",
        "pacing": (
            "Fast cuts (1.5-2.5s average). Punchlines land on the last frame of a shot, not the middle. "
            "Visual gags held just long enough to read. Title card bounces, doesn't sit."
        ),
        "camera": (
            "Mostly mediums and close-ups for reactions. Reaction shots are half the grammar. "
            "Minimal camera motion — the actors carry the movement."
        ),
        "cutting_rules": (
            "Cut on the punchline's final beat. Always show the reaction. Never linger past the joke."
        ),
        "vocabulary": ["deadpan", "chaos", "escalation", "absurd", "misdirect", "beat-and-flip"],
        "antipatterns": ["held silences", "dramatic lighting", "melancholy grades"],
        "typical_durations": [1, 4],
    },
    "scifi": {
        "label": "Sci-fi / speculative",
        "pacing": (
            "Establishing wides held 5-7s to let the world read. Character beats tighter (3-4s). "
            "Peak reveals held for emphasis. World-first grammar — worldbuilding over action in the first half."
        ),
        "camera": (
            "Smooth crane / dolly / drone reveals. Long takes for world exposure. "
            "Cinematic anamorphic framing when possible."
        ),
        "cutting_rules": (
            "Reveal shots hold until the viewer has understood what they're seeing. "
            "Don't rush wides. Tighten only when the story accelerates."
        ),
        "vocabulary": ["vast", "alien", "emergent", "beyond", "reveal", "scale", "impossible"],
        "antipatterns": ["rapid cutting in establishing sequences", "handheld wides", "murky imagery"],
        "typical_durations": [3, 8],
    },
    "documentary": {
        "label": "Documentary / nonfiction",
        "pacing": (
            "VO-forward. Shots serve the narration. Longer establishing (5-7s), tighter character close-ups (3-4s). "
            "Archival or interview cuts punctuate."
        ),
        "camera": (
            "Observational — handheld verité mixed with tripod-locked interview shots. "
            "Pans across stills where archival footage dominates."
        ),
        "cutting_rules": "Cut to whatever the VO names. Show, then tell, then show again.",
        "vocabulary": ["witness", "unfold", "record", "testament", "chronicle", "uncover"],
        "antipatterns": ["stylized action grammar", "dramatic Hollywood tropes", "narrative fiction structure"],
        "typical_durations": [4, 8],
    },
    "coming_of_age": {
        "label": "Coming-of-age / indie drama",
        "pacing": (
            "Unhurried. Sustained performance shots. Observational wides. "
            "Let silences hold. Cuts motivated by emotional turn, not plot advance."
        ),
        "camera": (
            "Natural-light handheld. Close-ups of small gestures (hands, breath). "
            "Static wides of environments. Often a recurring formal motif (specific focal length, specific angle)."
        ),
        "cutting_rules": (
            "Cut on change of feeling. Linger on faces finishing sentences. Don't cut on dialogue — let lines breathe."
        ),
        "vocabulary": ["tender", "fleeting", "returning", "small", "specific", "ordinary", "becoming"],
        "antipatterns": ["spectacle", "fast cuts", "plot-heavy exposition"],
        "typical_durations": [4, 9],
    },
}


def list_genres() -> list[dict]:
    """UI-friendly list of all genres."""
    return [{"id": k, "label": v["label"]} for k, v in GENRES.items()]


def get_rules(genre: str) -> dict:
    """Return the full rule dict for a genre; 'neutral' for unknowns."""
    return GENRES.get(genre) or GENRES["neutral"]


def system_prompt_block(genre: str) -> str:
    """Return a prompt-ready block for injection into storyboard / director /
    sweep system prompts. Empty string for 'neutral' (no bias applied)."""
    if not genre or genre == "neutral":
        return ""
    rules = get_rules(genre)
    dur_lo, dur_hi = rules["typical_durations"]
    return (
        f"# GENRE: {rules['label']}\n\n"
        f"Pacing: {rules['pacing']}\n"
        f"Camera: {rules['camera']}\n"
        f"Cutting rules: {rules['cutting_rules']}\n"
        f"Typical shot durations: {dur_lo}-{dur_hi}s\n"
        f"DO lean on: {', '.join(rules['vocabulary']) or '(none specific)'}\n"
        f"DON'T: {', '.join(rules['antipatterns']) or '(no specific antipatterns)'}\n"
    )
