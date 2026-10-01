"""Turn taking and speakable text for a phone call.

These are the in-app voice call's pieces (static/js/voiceCall.js: Vad,
speakableText, takeSentences) in Python, with the same numbers, so a call
on the phone ends a turn, hears a barge-in and reads a reply the same way a
call in the browser does. tests/test_phone_calls.py runs both on the same
inputs.
"""

import re
from typing import List, Optional, Tuple

# The in-app call's timings (voiceCall.js).
SILENCE_MS = 700
PREROLL_MS = 400
MIN_SPEECH_MS = 220
MAX_UTTERANCE_MS = 60000
ECHO_TAIL_MS = 300


class Vad:
    """Energy based voice activity detection. Feed it one RMS value per
    frame. The first `calibrate_ms` set the noise floor, which then drifts
    slowly while nobody talks. Speech starts after `onset_ms` above the
    threshold and ends after `silence_ms` below it, with some hysteresis.

    Phone audio is quieter and noisier than a laptop mic, and the line is
    often silent (comfort noise) at first, so the floor is capped the same
    way and the minimum threshold is the in-app one."""

    def __init__(self, silence_ms: int = SILENCE_MS, onset_ms: int = 120, calibrate_ms: int = 1000,
                 min_threshold: float = 0.012, ratio: float = 3):
        self.silence_ms = silence_ms
        self.onset_ms = onset_ms
        self.calibrate_ms = calibrate_ms
        self.min_threshold = min_threshold
        self.ratio = ratio
        self.reset(True)

    def reset(self, recalibrate: bool = False) -> None:
        if recalibrate:
            self.noise = 0.0
            self._cal_ms = 0.0
            self._cal_sum = 0.0
            self._cal_n = 0
            self.calibrated = False
        self.speaking = False
        self._above = 0.0
        self._below = 0.0
        self.speech_ms = 0.0

    @property
    def threshold(self) -> float:
        return max(self.min_threshold, self.noise * self.ratio)

    def push(self, level: float, dt_ms: float) -> Optional[str]:
        """'calibrated', 'start', 'end' or None."""
        if not self.calibrated:
            self._cal_ms += dt_ms
            self._cal_sum += level
            self._cal_n += 1
            if self._cal_ms >= self.calibrate_ms:
                self.noise = min(0.05, self._cal_sum / max(1, self._cal_n))
                self.calibrated = True
                return "calibrated"
            return None
        thr = self.threshold
        if not self.speaking:
            if level > thr:
                self._above += dt_ms
                if self._above >= self.onset_ms:
                    self.speaking = True
                    self._below = 0.0
                    self.speech_ms = self._above
                    return "start"
            else:
                self._above = 0.0
                self.noise = min(0.05, self.noise * 0.97 + level * 0.03)
            return None
        if level > thr * 0.75:
            self._below = 0.0
            self.speech_ms += dt_ms
        else:
            self._below += dt_ms
            if self._below >= self.silence_ms:
                self.speaking = False
                self._above = 0.0
                return "end"
        return None


# ── Reply text to speech ─────────────────────────────────────────────────

def speakable_text(raw: str) -> str:
    """A reply's markdown as plain words to read out: reasoning, code
    blocks and markup dropped. Stable on a growing stream, so an offset into
    a shorter prefix's result stays valid."""
    t = str(raw or "")
    t = re.sub(r"<think(?:ing)?>[\s\S]*?(?:</think(?:ing)?>|$)", " ", t, flags=re.I)
    t = re.sub(r"```[\s\S]*?(?:```|$)", " ", t)
    t = re.sub(r"</?[a-z][^>]{0,200}>", " ", t, flags=re.I)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = t.replace("`", "")
    t = re.sub(r"^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+)", "", t, flags=re.M)
    t = re.sub(r"\*{1,3}|~~|__", "", t)
    t = re.sub(r"^[ \t|:-]{3,}$", "", t, flags=re.M)
    t = t.replace("|", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n\s*", "\n", t)
    return t.lstrip()


_ABBR = re.compile(r"(?:^|[\s(])(?:mr|mrs|ms|dr|st|sr|jr|vs|etc|e\.g|i\.e|approx|fig|[a-z])\.$", re.I)
_WORDISH = re.compile(r"[^\W_]", re.U)


def take_sentences(plain: str, start: int = 0, final: bool = False) -> Tuple[List[str], int]:
    """Complete sentences in `plain` from index `start`: a sentence ends at
    . ! ? followed by a space, or at a line break. With `final`, the rest
    counts too. Returns (sentences, index just after the last one)."""
    out: List[str] = []
    i = start

    def push(s: str) -> None:
        x = re.sub(r"\s+", " ", s).strip()
        if _WORDISH.search(x):
            out.append(x)

    n = len(plain)
    while i < n:
        ch = plain[i]
        if ch == "\n":
            push(plain[start:i])
            start = i + 1
            i += 1
            continue
        if ch in ".!?…":
            j = i + 1
            while j < n and plain[j] in "\"')]’”.!?":
                j += 1
            if j >= n:
                break                       # wait to see what follows
            if plain[j].isspace():
                piece = plain[start:j]
                skip = ch == "." and (bool(_ABBR.search(piece.rstrip())) or bool(re.match(r"^\s*\d+\.$", piece)))
                if not skip:
                    push(piece)
                    start = j
                i = j
                continue
        i += 1
    if final and start < n:
        push(plain[start:])
        start = n
    return out, start
