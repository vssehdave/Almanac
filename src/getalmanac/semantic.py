"""Search by meaning: a small embedding model that runs on this machine.

Optional. `pip install "getalmanac[semantic]"` adds the packages, and `almanac semantic install`
downloads the model once (all-MiniLM-L6-v2, about 90 MB, CPU only). That download is the only time
Almanac contacts a host other than the upstream, and it sends no conversation text. The model is never
downloaded or loaded unless it is already there: without the packages or the model, or with
`memory.semantic` set to false, memory search stays keyword-based.

Each vector is a unit-length float32 array packed as bytes. It is stored beside the exchange it came
from with the embedder's name, so vectors made by different embedders are never compared. The packages
are imported only when the model loads, never when Almanac starts.
"""
import importlib.util
import os
import threading

from . import config, embed, report

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"  # the commit that was tested
NAME = "minilm-l6-v2-" + REVISION[:8]
FILES = ("tokenizer.json", "onnx/model.onnx")
PACKAGES = ("onnxruntime", "tokenizers", "numpy", "huggingface_hub")
MAX_TOKENS = 256  # the model's own limit; longer text is cut
MAX_TEXT = 1500  # characters of one exchange that are embedded

_lock = threading.Lock()
_loaded = {}  # "embedder" -> the embedder, or None when loading failed; set once per process


class Embedder:
    """`encode(texts)` gives one packed unit-length vector (bytes) per text. `name` says which model made it."""

    def __init__(self, name, encode):
        self.name, self.encode = name, encode


def missing() -> list:
    """The optional packages that are not installed."""
    return [name for name in PACKAGES if importlib.util.find_spec(name) is None]


def _cached(filename):
    """Path of a model file in the Hugging Face cache, or None. Reading the cache directly keeps
    huggingface_hub (slow to import) out of every run that is not a download."""
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    path = os.path.join(hub, "models--" + MODEL.replace("/", "--"), "snapshots", REVISION, *filename.split("/"))
    return path if os.path.isfile(path) else None


def present() -> bool:
    return all(_cached(name) for name in FILES)


def download() -> int:
    """Fetch the model files into the Hugging Face cache. Returns their size in bytes."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from huggingface_hub import hf_hub_download

    paths = [hf_hub_download(MODEL, name, revision=REVISION) for name in FILES]
    _loaded.clear()
    return sum(os.path.getsize(p) for p in paths)


def _build():
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(_cached("tokenizer.json"))
        if hasattr(tok, "enable_truncation"):
            tok.enable_truncation(MAX_TOKENS)
            tok.enable_padding()
        else:  # tokenizers 1.0: settings go on an editable tokenizer, then compile() makes one that encodes
            from tokenizers import Padding, Truncation

            tok.truncation = Truncation(max_length=MAX_TOKENS)
            tok.padding = Padding()
            tok = tok.compile()
        sess = ort.InferenceSession(_cached("onnx/model.onnx"), providers=["CPUExecutionProvider"])
        wants_types = "token_type_ids" in {i.name for i in sess.get_inputs()}
    except Exception as exc:  # a broken install or a damaged file: keyword search still works
        report.log_error("semantic:load", exc)  # load() calls this once per process
        return None

    def encode(texts):
        texts, out = list(texts), []
        for i in range(0, len(texts), 32):  # small batches keep memory flat
            enc = tok.encode_batch(texts[i:i + 32])
            ids = np.array([e.ids for e in enc], dtype="int64")
            mask = np.array([e.attention_mask for e in enc], dtype="int64")
            feed = {"input_ids": ids, "attention_mask": mask}
            if wants_types:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = sess.run(None, feed)[0]  # (batch, tokens, 384)
            mean = (hidden * mask[..., None]).sum(axis=1) / np.maximum(mask.sum(axis=1, keepdims=True), 1)
            norms = np.linalg.norm(mean, axis=1, keepdims=True)
            out += [row.tobytes() for row in (mean / np.where(norms == 0, 1, norms)).astype("float32")]
        return out

    return Embedder(NAME, encode)


def load():
    """The model, loaded once per process (about a second), or None when the packages or the model are
    missing or loading failed. A missing model is not remembered, so installing it takes effect at once."""
    with _lock:
        if "embedder" not in _loaded:
            if missing() or not present():
                return None
            _loaded["embedder"] = _build()
        return _loaded["embedder"]


def active():
    """The embedder to use now, or None: `memory.semantic` is false, or nothing to load."""
    return load() if config.load()["memory"]["semantic"] else None


def cosines(query, blobs) -> list:
    """The cosine of one packed unit vector against each of `blobs` (unit vectors, so a dot product)."""
    try:
        import numpy as np
    except ImportError:  # only a test embedder gets here: the real one needs numpy
        wanted = embed.unpack(query)
        return [embed.cosine(wanted, embed.unpack(blob)) for blob in blobs]
    matrix = np.frombuffer(b"".join(blobs), dtype="float32").reshape(len(blobs), -1)
    return (matrix @ np.frombuffer(query, dtype="float32")).tolist()
