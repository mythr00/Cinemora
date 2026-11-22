"""Rule-based English syllabifier (Wave 1 captions, plan section E.3).

No dependencies, no downloads, no word lists loaded at runtime --
the exception table below is bundled in this module.

Honesty note: syllable boundaries produced here are DERIVED from a
word's spelling. They are never measured from audio. Callers that
turn these spans into caption timing must stamp that timing as
derived (the caption renderer writes ``; SyllableTiming: derived``
into the ASS header when it uses this module).

API:
    syllabify(word) -> list[str]
        Syllables of ``word``; ``"".join(syllabify(w)) == w`` for
        ordinary words (leading/trailing punctuation rides on the
        first/last syllable).
    syllable_spans(word, start, end) -> list[(syllable, start, end)]
        The word's [start, end] interval distributed across its
        syllables, weighted by syllable character length. Spans are
        contiguous and sum exactly to [start, end].
"""

# Common irregulars whose spelling defeats the vowel-group rules
# (silent letters, syllabic consonants, compressed vowels). Keys are
# the lowercase letter-core; values must join back to the key.
EXCEPTIONS = {
    "business": ["busi", "ness"],
    "camera": ["cam", "era"],
    "chocolate": ["choco", "late"],
    "comfortable": ["com", "forta", "ble"],
    "completely": ["com", "plete", "ly"],
    "different": ["dif", "ferent"],
    "evening": ["eve", "ning"],
    "every": ["ev", "ery"],
    "family": ["fam", "ily"],
    "favorite": ["fav", "orite"],
    "favourite": ["fav", "ourite"],
    "generally": ["gen", "erally"],
    "interesting": ["in", "terest", "ing"],
    "literature": ["lit", "era", "ture"],
    "several": ["sev", "eral"],
    "temperature": ["tem", "pera", "ture"],
    "vegetable": ["vege", "ta", "ble"],
    "wednesday": ["wednes", "day"],
    "separate": ["sep", "arate"],
    "desperate": ["des", "perate"],
    "reference": ["ref", "erence"],
    "preference": ["pref", "erence"],
    "average": ["av", "erage"],
    "leverage": ["lev", "erage"],
    "coverage": ["cov", "erage"],
    "actually": ["ac", "tually"],
    "basically": ["ba", "sically"],
    "probably": ["prob", "ably"],
    "diamond": ["dia", "mond"],
    "poetry": ["po", "etry"],
    "being": ["be", "ing"],
    "science": ["sci", "ence"],
    "society": ["so", "ci", "ety"],
    "quiet": ["qui", "et"],
    "area": ["ar", "ea"],
    "idea": ["i", "dea"],
    "video": ["vid", "eo"],
    "radio": ["ra", "dio"],
    "audio": ["au", "dio"],
    "create": ["cre", "ate"],
    "client": ["cli", "ent"],
    "diet": ["di", "et"],
    "lion": ["li", "on"],
    "riot": ["ri", "ot"],
    "trial": ["tri", "al"],
    "usual": ["u", "sual"],
    "fire": ["fire"],
    "hour": ["hour"],
    "our": ["our"],
}

_VOWELS = set("aeiou")
# Multi-letter onsets kept together on the following syllable where
# onset maximisation allows.
_ONSETS = {
    "bl", "br", "cl", "cr", "dr", "dw", "fl", "fr", "gl", "gr",
    "kl", "kr", "pl", "pr", "tr", "tw", "kw", "qu", "sm", "sn",
    "sp", "st", "sk", "sl", "sw", "sf", "ch", "sh", "th", "ph",
    "wh", "gh", "scr", "spl", "spr", "squ", "str", "thr", "shr",
    "sch",
}
_DIGRAPHS = {"ch", "sh", "th", "ph", "wh", "ck", "ng", "gh", "qu"}


def _is_vowel(core, i):
    ch = core[i]
    if ch in _VOWELS:
        return True
    if ch == "y":
        # y is a vowel when it does not start the word and does not
        # precede another vowel (duty, documentary's final -ry).
        if i == 0:
            return False
        if i + 1 < len(core) and core[i + 1] in _VOWELS:
            return False
        return True
    return False


def _nuclei(core):
    """Vowel-group spans [(start, end), ...] = syllable nuclei."""
    groups = []
    i = 0
    while i < len(core):
        if _is_vowel(core, i):
            j = i + 1
            while j < len(core) and _is_vowel(core, j):
                j += 1
            groups.append((i, j))
            i = j
        else:
            i += 1
    if len(groups) > 1:
        # Silent final -e: "make", "write" -- but consonant + "le"
        # is syllabic ("table", "people") and -ee/-ye/-oe are not
        # silent.
        s, e = groups[-1]
        if (
            core[s:e] == "e"
            and e == len(core)
            and not core.endswith("le")
        ):
            groups.pop()
        elif (
            core[s:e] == "e"
            and e == len(core) - 1
            and core.endswith("es")
            and len(groups) > 1
        ):
            groups.pop()
    return groups


def _split_point(core, left_end, right_start):
    """Boundary index between two nuclei: onset maximisation.

    The consonant cluster core[left_end:right_start] is split so as
    much of it as forms a valid onset goes to the next syllable.
    """
    cluster = core[left_end:right_start]
    n = len(cluster)
    if n == 0:
        return left_end
    if n == 1:
        return left_end  # single consonant onsets the next syllable
    # Longest suffix (<=3) that is a valid onset goes right.
    onset_len = 1
    for size in (3, 2):
        if n >= size and cluster[n - size:] in _ONSETS:
            onset_len = size
            break
    point = right_start - onset_len
    # Never split inside a digraph.
    if point - 1 >= left_end and core[point - 1:point + 1] in _DIGRAPHS:
        point += 1
        if point > right_start - 1:
            point = right_start - 1
    return point


def syllabify(word):
    """Split ``word`` into syllables (see module docstring)."""
    if not word:
        return []
    text = str(word)
    # Letter core; punctuation/apostrophes ride on edge syllables.
    start = 0
    while start < len(text) and not text[start].isalpha():
        start += 1
    end = len(text)
    while end > start and not text[end - 1].isalpha():
        end -= 1
    if start >= end:
        return [text]
    prefix, core_raw, suffix = text[:start], text[start:end], text[end:]
    core = core_raw.lower()
    # Internal apostrophes (don't) are not letters for nucleus
    # purposes but stay in the returned syllables via index mapping:
    # work on the apostrophe-free core and remember positions.
    if "'" in core:
        squeezed = core.replace("'", "")
        parts = syllabify(squeezed)
        # Re-insert apostrophes at their original offsets.
        apos = [i for i, c in enumerate(core) if c == "'"]
        joined = list("".join(parts))
        for pos in apos:
            joined.insert(pos, "'")
        rebuilt, idx = [], 0
        for part in parts:
            take = len(part)
            extra = sum(1 for p in apos if idx <= p < idx + take + 1)
            seg = "".join(joined[idx:idx + take + extra])
            # Simpler correctness guard: fall through below if the
            # reconstruction does not round-trip.
            if seg:
                rebuilt.append(seg)
            idx += take + extra
        if "".join(rebuilt) == core_raw:
            if rebuilt:
                rebuilt[0] = prefix + rebuilt[0]
                rebuilt[-1] = rebuilt[-1] + suffix
            return rebuilt
        return [text]

    if core in EXCEPTIONS:
        parts = list(EXCEPTIONS[core])
        if "".join(parts) != core:  # table sanity; never mis-split
            parts = [core]
    else:
        groups = _nuclei(core)
        if len(groups) <= 1:
            parts = [core]
        else:
            bounds = [0]
            for k in range(len(groups) - 1):
                bounds.append(
                    _split_point(core, groups[k][1], groups[k + 1][0])
                )
            bounds.append(len(core))
            parts = [
                core[bounds[i]:bounds[i + 1]]
                for i in range(len(bounds) - 1)
            ]
            parts = [p for p in parts if p] or [core]
    # Restore original casing on the first letter(s).
    out = []
    pos = 0
    for part in parts:
        out.append(core_raw[pos:pos + len(part)])
        pos += len(part)
    if "".join(out) != core_raw:
        out = [core_raw]
    if out:
        out[0] = prefix + out[0]
        out[-1] = out[-1] + suffix
    return out


def syllable_spans(word, start, end):
    """Distribute [start, end] across ``word``'s syllables.

    Weighted by syllable character length; spans are contiguous and
    the final span ends exactly at ``end``.
    """
    start = float(start)
    end = float(end)
    parts = syllabify(word)
    if not parts:
        return []
    if len(parts) == 1 or end <= start:
        return [(parts[0], start, end)] if len(parts) == 1 else [
            (p, start, start) for p in parts
        ]
    weights = [max(1, len(p)) for p in parts]
    total_weight = sum(weights)
    duration = end - start
    spans = []
    cursor = start
    cumulative = 0
    for i, part in enumerate(parts):
        cumulative += weights[i]
        if i == len(parts) - 1:
            boundary = end
        else:
            boundary = start + duration * cumulative / total_weight
        spans.append((part, cursor, boundary))
        cursor = boundary
    return spans
