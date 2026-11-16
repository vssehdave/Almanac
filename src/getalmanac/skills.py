"""Scan skills and plugins with SkillSpector before they are installed.

The scanner runs as a separate program and is never imported. Everything fetched is untrusted: it is
fetched into a quarantine folder, nothing in it is ever run, and only folders holding a SKILL.md are
copied out, without their links, and only after a SAFE scan.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

VERDICTS = {"SAFE": 0, "CAUTION": 1, "DO_NOT_INSTALL": 2}
ERROR = 3  # the scanner is missing, failed, or gave an answer that cannot be read
INSTALL = "uv tool install git+https://github.com/NVIDIA/skillspector.git"
SEVERITY = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
SKIP = {".git", "node_modules"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SCAN_SECONDS = 900  # one plugin took over 3 minutes on the owner's machine


def claude_dir() -> Path:
    """Claude Code's config folder: CLAUDE_CONFIG_DIR when set (a second account), else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def scanner():
    """The scanner command as a list, or None. ALMANAC_SKILLSPECTOR overrides it (a path, or a JSON list)."""
    override = os.environ.get("ALMANAC_SKILLSPECTOR")
    if override:
        return json.loads(override) if override.startswith("[") else [override]
    found = shutil.which("skillspector")
    if found:
        return [found]
    for cand in (Path.home() / ".local/bin/skillspector.exe", Path.home() / ".local/bin/skillspector"):
        if cand.exists():
            return [str(cand)]
    return None


LINK_TAGS = {getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C), getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)}


def linked(path) -> bool:
    """A symlink or a Windows junction (Python 3.10 cannot tell the latter). Neither is followed or copied."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    # only link tags: OneDrive's cloud files are reparse points too, and are plain files
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in LINK_TAGS


def result(verdict, score=None, issues=(), error=""):
    return {"verdict": verdict, "score": score, "issues": list(issues), "error": error}


def parse(code: int, out: bytes, err: bytes) -> dict:
    """The scanner's answer. The verdict comes from the report, not the exit code (0 covers SAFE and CAUTION)."""
    tail = (err or b"").decode("utf-8", "replace").strip()[-400:]
    if code == 2:
        return result("ERROR", error=tail or "the scanner failed (exit 2)")
    try:
        data = json.loads(out)
        risk = data["risk_assessment"]
        verdict, score = risk["recommendation"], int(risk["score"])
    except (ValueError, KeyError, TypeError):
        return result("ERROR", error=f"the scanner gave no readable report (exit {code}) {tail}".strip())
    if verdict not in VERDICTS:
        return result("ERROR", error=f"unknown verdict {verdict!r}")
    if data.get("execution_successful") is False:
        return result("ERROR", score, error="the scan did not finish")
    seen, issues = set(), []
    for item in data.get("issues") or []:
        loc = item.get("location") or {}
        issue = {"severity": str(item.get("severity", "?")), "category": str(item.get("category", "?")),
                 "file": str(loc.get("file", "?")), "line": loc.get("start_line")}
        key = tuple(issue.values())
        if key not in seen:
            seen.add(key)
            issues.append(issue)
    issues.sort(key=lambda i: SEVERITY.get(i["severity"].upper(), 9))
    complete = (data.get("analysis_completeness") or {}).get("is_complete", True)
    if verdict == "SAFE" and complete is False:
        return result("CAUTION", score, issues, "the scan did not cover every file")
    return result(verdict, score, issues)


def scan(path) -> dict:
    """Scan one local folder or file, static checks only (--no-llm: no file contents go to a model)."""
    cmd = scanner()
    if not cmd:
        return result("ERROR", error="missing")
    # the scanner refuses a junction, and ~/.claude/skills entries often are one
    target = os.path.realpath(path)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([*cmd, "scan", target, "--no-llm", "-f", "json"], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=SCAN_SECONDS, env=env, cwd=tempfile.gettempdir())
    except FileNotFoundError:
        return result("ERROR", error="missing")
    except subprocess.TimeoutExpired:
        return result("ERROR", error=f"the scan took longer than {SCAN_SECONDS // 60} minutes")
    except OSError as exc:
        return result("ERROR", error=str(exc))
    return parse(proc.returncode, proc.stdout, proc.stderr)


def code(res) -> int:
    return VERDICTS.get(res["verdict"], ERROR)


def render(res, label, limit=None) -> str:
    if res["error"] == "missing":
        return f"{label}: not scanned, SkillSpector is not installed. Install it with:\n  {INSTALL}"
    if res["verdict"] == "ERROR":
        return f"{label}: not scanned, the scanner failed: {res['error']}"
    lines = [f"{res['verdict']}  score {res['score']}/100  {label}" + (f"  ({res['error']})" if res["error"] else "")]
    shown = res["issues"][:limit] if limit else res["issues"]
    lines += [f"  {i['severity']:<8} {i['category']}  {i['file']}" + (f":{i['line']}" if i["line"] else "") for i in shown]
    if len(shown) < len(res["issues"]):
        lines.append(f"  ... and {len(res['issues']) - len(shown)} more")
    return "\n".join(lines)


def tree_hash(path) -> str:
    """sha256 over sorted relative paths and file bytes, skipping .git, node_modules and links."""
    root, digest = os.path.realpath(path), hashlib.sha256()
    if os.path.isfile(root):
        with open(root, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not linked(os.path.join(here, d)))
        for name in sorted(files):
            full = os.path.join(here, name)
            if linked(full):
                continue
            digest.update(os.path.relpath(full, root).replace(os.sep, "/").encode("utf-8", "surrogatepass") + b"\0")
            try:
                with open(full, "rb") as handle:
                    digest.update(hashlib.sha256(handle.read()).digest())
            except OSError:
                digest.update(b"unreadable")
    return digest.hexdigest()


def _no_links(here, names):
    return [n for n in names if linked(os.path.join(here, n)) or n == ".git"]


def copy(src, dest):
    """Copy a folder without its links (a link could point anywhere on this machine) or .git."""
    shutil.copytree(src, dest, ignore=_no_links, symlinks=False)


def remove(path):
    """Delete a folder git made, whose object files are read-only on Windows."""
    def again(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)

    if os.path.exists(path):
        shutil.rmtree(path, onerror=again)


def quarantine() -> Path:
    path = config.home() / "quarantine" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    path.mkdir(parents=True)
    return path


def _git(args, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=600)


def fetch(source: str, ref, into: Path):
    """Put a copy of `source` (an https git URL or a local folder) in `into`/tree. Returns (tree, commit or None).

    Hooks point at an empty folder, links are checked out as plain files, submodules and LFS are skipped,
    so nothing from the fetched tree runs.
    """
    tree = into / "tree"
    if ref and not SHA.match(ref):
        raise ValueError("--ref must be a full 40-character commit SHA")
    if os.path.isdir(source) and not ref:
        copy(source, tree)
        return tree, None
    if not source.startswith("https://"):
        raise ValueError("the source must be an https:// git URL or an existing local folder")
    hooks = into / "no-hooks"
    hooks.mkdir()
    safe = ["-c", f"core.hooksPath={hooks}", "-c", "core.symlinks=false", "-c", "submodule.recurse=false",
            "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never"]
    if ref:
        steps = [["init", "-q", str(tree)], [*safe, "-C", str(tree), "fetch", "-q", "--depth", "1", "--", source, ref],
                 [*safe, "-C", str(tree), "checkout", "-q", "--detach", "FETCH_HEAD"]]
    else:
        steps = [[*safe, "clone", "-q", "--depth", "1", "--no-recurse-submodules", "--", source, str(tree)]]
    for step in steps:
        proc = _git(step)
        if proc.returncode:
            raise ValueError(f"git {step[step.index('-q') - 1]} failed: {proc.stderr.strip()[-300:]}")
    head = _git(["-C", str(tree), "rev-parse", "HEAD"]).stdout.strip()
    return tree, head or None


def source_name(source: str) -> str:
    return source.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].removesuffix(".git")


def find_skills(tree: Path, wanted=None, default=""):
    """[(name, folder)] for each folder holding a SKILL.md, or only the `wanted` ones (paths or folder names)."""
    found = []
    for here, dirs, files in os.walk(tree):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not linked(os.path.join(here, d)))
        if "SKILL.md" in files and not linked(os.path.join(here, "SKILL.md")):
            found.append(Path(here))
            dirs[:] = []  # a skill's own sub-folders are part of it
    rel = {p: p.relative_to(tree).as_posix() for p in found}
    if wanted:
        picked = []
        for want in wanted:
            parts = want.replace("\\", "/").split("/")
            if os.path.isabs(want) or want.startswith(("/", "\\")) or ".." in parts or ":" in want or not want.strip("/"):
                raise ValueError(f"skill {want!r}: give a folder inside the source, without '..' or a drive")
            exact = [p for p in found if rel[p] == want.strip("/")]
            match = exact or [p for p in found if p.name == parts[-1]]
            if len(match) != 1:
                raise ValueError(f"skill {want!r}: " + ("not found" if not match else
                                 "matches " + ", ".join(rel[p] for p in match) + "; give the full path"))
            picked.append(match[0])
        found = picked
    out = []
    for folder in found:
        name = default if folder == tree else folder.name
        if not NAME.match(name or "") or ".." in name:
            raise ValueError(f"skill folder name {name!r} is not allowed")
        if name in [n for n, _ in out]:
            raise ValueError(f"two skills are called {name}; pick one with --skill")
        out.append((name, folder))
    return out


def _json(path):
    try:
        if path.is_file() and not linked(path) and path.stat().st_size < 2_000_000:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def plugin_parts(tree: Path):
    """What a Claude Code plugin at `tree` declares that runs code: {"hooks": [...], "mcp": [...]}, or None."""
    if not (tree / ".claude-plugin").is_dir():
        return None
    manifest = _json(tree / ".claude-plugin" / "plugin.json")
    hooks, mcp = [], []
    sources = [_json(tree / "hooks" / "hooks.json")]
    declared = manifest.get("hooks")
    for item in declared if isinstance(declared, list) else [declared]:
        if isinstance(item, dict):
            sources.append(item)
        elif isinstance(item, str):
            hooks.append(f"hooks file {item}")
    for data in sources:
        for event, entries in (data.get("hooks", data) or {}).items():
            for entry in entries if isinstance(entries, list) else []:
                for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                    if isinstance(hook, dict):
                        hooks.append(f"{event}: {str(hook.get('command', hook.get('type', '?')))[:120]}")
    servers = dict(_json(tree / ".mcp.json").get("mcpServers") or {})
    if isinstance(manifest.get("mcpServers"), dict):
        servers.update(manifest["mcpServers"])
    for name, spec in servers.items():
        spec = spec if isinstance(spec, dict) else {}
        mcp.append(f"{name}: {' '.join(str(x) for x in [spec.get('command', spec.get('url', '?')), *(spec.get('args') or [])])[:160]}")
    return {"hooks": hooks, "mcp": mcp}


def plugin_commands(tree: Path, source: str):
    """The `claude plugin` commands that would install this plugin, read from its marketplace file."""
    market = _json(tree / ".claude-plugin" / "marketplace.json")
    names = [p.get("name") for p in market.get("plugins") or [] if isinstance(p, dict) and p.get("name")]
    names = names or [_json(tree / ".claude-plugin" / "plugin.json").get("name") or source_name(source)]
    where = market.get("name") or source_name(source)
    return [f"claude plugin marketplace add {source}"] + [f"claude plugin install {n}@{where}" for n in names]


def _records_path():
    return config.home() / "skills.json"


def load_json(path, empty):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(empty)) else empty
    except (OSError, ValueError):
        return empty


def save_json(path, data):
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def add(source, wanted=None, ref=None, accept_caution=False, replace=False, plugin=False, out=print) -> int:
    """Fetch, scan and install skill folders. Returns the worst verdict's exit code (3 on any failure)."""
    box = quarantine()
    try:
        try:
            tree, commit = fetch(source, ref, box)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            out(f"almanac: could not fetch {source}: {exc}")
            return ERROR
        parts = plugin_parts(tree)
        if plugin or (parts and (parts["hooks"] or parts["mcp"]) and not wanted):
            return _plugin(tree, source, parts, out)
        try:
            skills = find_skills(tree, wanted, source_name(source))
        except ValueError as exc:
            out(f"almanac: {exc}")
            return ERROR
        if not skills:
            out(f"almanac: no folder with a SKILL.md in {source}")
            return ERROR
        dest_root = claude_dir() / "skills"
        for name, _ in skills:
            dest = dest_root / name
            if linked(dest):
                out(f"almanac: {dest} is a link to another folder; remove it yourself first")
                return ERROR
            if dest.exists() and not replace:
                out(f"almanac: a skill called {name} is already installed at {dest}; add --replace to overwrite it")
                return ERROR
        results = []
        for name, folder in skills:
            res = scan(folder)
            out(render(res, name))
            if res["error"] == "missing":
                return ERROR
            results.append(res)
        worst = max(code(r) for r in results)
        if worst >= VERDICTS["DO_NOT_INSTALL"]:
            out("almanac: refused. Nothing was installed. A DO_NOT_INSTALL verdict or a failed scan cannot be overridden.")
            return worst
        if worst == VERDICTS["CAUTION"] and not accept_caution:
            out("almanac: refused. Nothing was installed. Read the issues above; add --accept-caution to install anyway.")
            return worst
        dest_root.mkdir(parents=True, exist_ok=True)
        records = load_json(_records_path(), {})
        installed = records.setdefault("skills", {})
        for (name, folder), res in zip(skills, results):
            dest, tmp = dest_root / name, dest_root / f".almanac-new-{name}"
            remove(tmp)
            copy(folder, tmp)
            remove(dest)
            os.replace(tmp, dest)
            digest = tree_hash(dest)
            installed[name] = {"name": name, "source": source, "commit": commit, "tree": digest, "score": res["score"],
                               "verdict": res["verdict"], "date": time.strftime("%Y-%m-%d %H:%M:%S")}
            out(f"installed {name} -> {dest}")
        save_json(_records_path(), records)
        return worst
    finally:
        remove(box)


def _plugin(tree, source, parts, out):
    """Scan a plugin and show what it would run. It is never installed here."""
    res = scan(tree)
    out(render(res, source_name(source)))
    parts = parts or {"hooks": [], "mcp": []}
    out(f"This is a Claude Code plugin. Almanac does not install plugins; it declares "
        f"{len(parts['hooks'])} hook(s) and {len(parts['mcp'])} MCP server(s):")
    for line in parts["hooks"] + parts["mcp"]:
        out(f"  {line}")
    out("To install it yourself, after reading the above:")
    for line in plugin_commands(tree, source):
        out(f"  {line}")
    return code(res)

