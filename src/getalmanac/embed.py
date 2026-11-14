"""Text vectors made on this machine with no model and no download.

Words and character trigrams are hashed into a fixed number of buckets (feature
hashing). Two texts that share vocabulary, or near-spellings of it, get a high
cosine. It is lexical, not semantic: it will not know that "car" and
"automobile" are related. It is deterministic and needs nothing installed.
"""
import math
import zlib
from array import array

from .match import tokens

DIM = 256
NAME = f"hash{DIM}"


def _bucket(feature: str):
    h = zlib.crc32(feature.encode("utf-8", "surrogatepass"))
    return h % DIM, (1.0 if (h >> 16) & 1 else -1.0)


def embed(text: str) -> list:
    """A unit-length vector for `text` (all zeros when it has no usable words)."""
    counts = {}
    for word in tokens(text):
        counts[word] = counts.get(word, 0) + 1.0
        if len(word) > 4:
            for i in range(len(word) - 2):
                gram = "#" + word[i:i + 3]
                counts[gram] = counts.get(gram, 0) + 0.3
    vec = [0.0] * DIM
    for feature, count in counts.items():
        index, sign = _bucket(feature)
        vec[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def mean(vectors) -> list:
    """The unit-length average of unit vectors."""
    total = [sum(col) for col in zip(*vectors)] if vectors else [0.0] * DIM
    norm = math.sqrt(sum(v * v for v in total))
    return [v / norm for v in total] if norm else total


def pack(vec) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob) -> list:
    out = array("f")
    out.frombytes(blob or b"")
    return list(out)
