"""Keyword matching: which catalog entries does a message call for?

BM25 over a small catalog (skills, tools). Deterministic and dependency-free, so
the same message always selects the same entries.
"""
import math
import re

STOP = frozenset("""
a about after all also an and any are as at be been before but by can could did do does doing done each either
else even every for from get give go had has have he her here him his how i if in into is it its just let like
make many may me might more most much must my need new no not now of off on once one only or other our out over
own please same she should so some such than that the their them then there these they thing this those through
to too try under up us use used using very want was way we were what when where which while who why will with
would yes you your claude user users file files code task tasks work help helps run running thing things whether
whenever triggers trigger asks ask asked mention mentions even something anything
""".split())
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _stem(word: str) -> str:
    for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3 and not word.endswith("ss"):
            return word[: -len(suffix)] + keep
    return word


def tokens(text: str) -> list:
    words = _WORD.findall(_CAMEL.sub(" ", text or "").lower())
    return [_stem(w) for w in words if len(w) > 1 and w not in STOP]


def _name_tokens(name: str) -> set:
    """A name's words, plus each run of it squashed together ("PowerShell" -> power, shell, powershell)."""
    out = set(tokens(name.replace(":", " ")))
    for part in re.split(r"[:/\s]+", name):
        squashed = re.sub(r"[^a-z0-9]", "", part.lower())
        if len(squashed) > 2:
            out.add(_stem(squashed))
    return out


class Index:
    """BM25 over `{name: text}`. The name's own words count three times."""

    K1, B = 1.4, 0.6

    def __init__(self, docs: dict):
        self.names = list(docs)
        self.tf, self.df, self.name_tokens = {}, {}, {}
        for name, text in docs.items():
            counts = {}
            self.name_tokens[name] = _name_tokens(name)
            for tok in tokens(text) + list(self.name_tokens[name]) * 3:
                counts[tok] = counts.get(tok, 0) + 1
            self.tf[name] = counts
            for tok in counts:
                self.df[tok] = self.df.get(tok, 0) + 1
        self.length = {name: sum(c.values()) for name, c in self.tf.items()}
        self.avg = sum(self.length.values()) / max(1, len(self.length))
        # the weight of a word found in exactly one entry: the unit scores are judged in,
        # so the same bar works for a catalog of five and of five hundred
        self.unit = math.log(1 + (len(self.names) - 0.5) / 1.5)

    def named(self, query: str) -> list:
        """Entries whose own name shares a word with the query."""
        terms = set(tokens(query))
        return [name for name in self.names if terms & self.name_tokens[name]]

    def search(self, query: str, limit=6, strength=1.6, min_terms=2) -> list:
        """[(name, score)] best first.

        A hit needs two distinct query words and a score of `strength` rare words,
        or one word from the entry's own name and half that score.
        """
        terms = set(tokens(query))
        total, bar = len(self.names), strength * self.unit
        hits = []
        for name in self.names:
            counts, score, matched = self.tf[name], 0.0, 0
            for term in terms:
                tf = counts.get(term)
                if not tf:
                    continue
                matched += 1
                idf = math.log(1 + (total - self.df[term] + 0.5) / (self.df[term] + 0.5))
                score += idf * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * self.length[name] / self.avg))
            named = bool(terms & self.name_tokens[name])
            if (named and score >= bar / 2) or (score >= bar and matched >= min_terms):
                hits.append((name, round(score, 3)))
        hits.sort(key=lambda h: (-h[1], h[0]))
        if hits:  # drop the long tail: keep what is close to the best match
            hits = [h for h in hits if h[1] >= hits[0][1] * 0.55]
        return hits[:limit]
