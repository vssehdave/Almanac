"""Scan skills and plugins with SkillSpector before they are installed.

The scanner runs as a separate program and is never imported. Everything fetched is untrusted: it is
fetched into a quarantine folder, nothing in it is ever run, and only folders holding a SKILL.md are
copied out, without their links, and only after a SAFE scan.
"""
import hashlib
import io
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


def offer_scanner(ask, out=print) -> None:
    """When SkillSpector is missing, offer to run its install command; runs it only on a yes."""
    if scanner() or not ask:
        return
    if not shutil.which("uv"):
        out(f"almanac: SkillSpector is not installed, and uv is not found to install it. Install uv, then: {INSTALL}")
        return
    if ask(f"SkillSpector scans skills before they are installed and is not installed. Run `{INSTALL}` now? [y/N] "):
        try:
            done = subprocess.run(INSTALL.split(), stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            out(f"almanac: could not run the install: {exc}")
            return
        out("SkillSpector installed." if done and scanner() else "almanac: the install did not finish; nothing was added.")


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
    digest = tree_hash(target)
    cache = load_json(_cache_path(), {})
    if digest and cache.get(digest, {}).get("verdict") in VERDICTS:
        return cache[digest]
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
    res = parse(proc.returncode, proc.stdout, proc.stderr)
    if digest and res["verdict"] != "ERROR":  # a failed scan is never remembered
        cache[digest] = res
        save_json(_cache_path(), cache)
    return res


def _cache_path():
    return config.home() / "scan-cache.json"


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


def tree_hash(path, deadline=None):
    """sha256 over sorted relative paths and file bytes, skipping .git, node_modules and links.

    None when time.monotonic() passes `deadline` before the last file, so one big folder cannot hold up a start.
    """
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
            if deadline is not None and time.monotonic() > deadline:
                return None
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
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=again)
        else:
            shutil.rmtree(path, onerror=again)


def quarantine() -> Path:
    path = config.home() / "quarantine" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    path.mkdir(parents=True)
    return path


def _git(args, cwd=None, timeout=600):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=timeout)


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


def mcp_servers(tree: Path) -> dict:
    """The MCP servers a plugin declares, in .mcp.json or its manifest."""
    servers = dict(_json(tree / ".mcp.json").get("mcpServers") or {})
    manifest = _json(tree / ".claude-plugin" / "plugin.json").get("mcpServers")
    if isinstance(manifest, dict):
        servers.update(manifest)
    return servers


def hook_lines(data) -> list:
    """One "Event: command" line per hook in a parsed hooks file."""
    lines = []
    for event, entries in (data.get("hooks", data) or {}).items():
        for entry in entries if isinstance(entries, list) else []:
            for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                if isinstance(hook, dict):
                    lines.append(f"{event}: {str(hook.get('command', hook.get('type', '?')))[:120]}")
    return lines


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
        hooks += hook_lines(data)
    for name, spec in mcp_servers(tree).items():
        spec = spec if isinstance(spec, dict) else {}
        mcp.append(f"{name}: {' '.join(str(x) for x in [spec.get('command', spec.get('url', '?')), *(spec.get('args') or [])])[:160]}")
    return {"hooks": hooks, "mcp": mcp}


HOOK_PATH = re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/([\w./@+-]+)")


def stage_hooks(tree: Path, dest: Path) -> list:
    """Copy hooks/hooks.json and the plugin files its commands name into `dest`. Returns the hook lines kept."""
    file = tree / "hooks" / "hooks.json"
    if not file.is_file() or linked(file):
        return []
    root = os.path.realpath(tree)
    for rel in sorted(set(HOOK_PATH.findall(file.read_text(encoding="utf-8", errors="replace")))):
        parts = rel.split("/")
        if ".." in parts or SKIP & set(parts):  # the scan hash ignores these, so they would go in unscanned
            print(f"  hook file not kept: {rel} (outside the plugin or in a folder the scan skips)")
            continue
        src = tree / rel
        if os.path.realpath(src).startswith(root + os.sep) and src.is_file() and not linked(src):
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)
    (dest / "hooks").mkdir(exist_ok=True)
    shutil.copyfile(file, dest / "hooks" / "hooks.json")
    return hook_lines(_json(file))


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


OVERRIDE_NOTE = "installed over DO_NOT_INSTALL by owner"


def _check(targets, accept_caution, ask, out, confirm=None):
    """Scan each (label, folder) and show it. Returns (results, stop): the exit code to stop with, or None to go on.

    `ask` is an optional yes/no question to the user, for a CAUTION verdict. With `confirm` (a strict typed-yes question,
    given only by the owner's flag), a DO_NOT_INSTALL verdict can be installed; a failed scan never can.
    """
    results = []
    for label, folder in targets:
        res = scan(folder)
        out(render(res, label))
        if res["error"] == "missing":
            return results, ERROR
        results.append(res)
    worst = max(code(r) for r in results)
    failed = any(r["error"] for r in results) or worst > VERDICTS["DO_NOT_INSTALL"]
    if worst >= VERDICTS["DO_NOT_INSTALL"] and (failed or not (confirm and confirm("Install a DO_NOT_INSTALL item anyway? Type yes: "))):
        out("almanac: refused. Nothing was installed. A failed scan cannot be overridden; "
            "for a DO_NOT_INSTALL verdict only the owner can, with --accept-danger.")
        return results, worst
    if worst == VERDICTS["CAUTION"] and not accept_caution and not (ask and ask("Install anyway? [y/N] ")):
        out("almanac: refused. Nothing was installed. Read the issues above; add --accept-caution to install anyway.")
        return results, worst
    return results, None


def add(source, wanted=None, ref=None, accept_caution=False, replace=False, plugin=False, out=print, bundle=False,
        name=None, ask=None, hooks=False, confirm=None) -> int:
    """Fetch, scan and install skill folders. Returns the worst verdict's exit code (3 on any failure).

    `bundle` installs them as a plugin in the Almanac bundle instead (see `_bundle`).
    """
    box = quarantine()
    try:
        try:
            tree, commit = fetch(source, ref, box)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            out(f"almanac: could not fetch {source}: {exc}")
            return ERROR
        if bundle:
            return _bundle(tree, commit, source, wanted, name, accept_caution, replace, ask, box, out, hooks, confirm)
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
        results, stop = _check(skills, accept_caution, ask, out, confirm)
        if stop is not None:
            return stop
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
            if code(res) == VERDICTS["DO_NOT_INSTALL"]:
                installed[name]["note"] = OVERRIDE_NOTE
            note_scan(dest, "skill", digest, res, name)
            out(f"installed {name} -> {dest}")
        save_json(_records_path(), records)
        return max(code(r) for r in results)
    finally:
        remove(box)


LICENCES = (("Apache-2.0", "apache license"), ("MIT", "permission is hereby granted, free of charge"),
            ("BSD", "redistribution and use in source and binary forms"), ("ISC", "isc license"),
            ("MPL-2.0", "mozilla public license"), ("GPL", "gnu general public license"),
            ("Unlicense", "free and unencumbered software"))
PINNED = re.compile(r"(@|==)v?\d+\.\d+\.\d+(?:[-+][\w.]+)?\Z")  # pkg@1.2.3 (npx) or pkg==1.2.3 (uvx)
SHIPPED = Path(__file__).parent / "bundle"
BUNDLE_HEAD = ("# Adopted skills and plugins\n\nEach folder here is a plugin Almanac loads into every session. Each was scanned "
               "and its findings read before it was installed.\n\n| Folder | From | Commit | Licence | Changed |\n|---|---|---|---|---|\n")


def bundle_dir() -> Path:
    return config.home() / "bundle"


def find_licence(tree: Path):
    """(name, file) of the licence file at the top of `tree`; the name is "unknown" when the text is not a common one."""
    for entry in sorted(tree.iterdir()):
        if entry.is_file() and not linked(entry) and entry.name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
            text = entry.read_text(encoding="utf-8", errors="replace").lower()
            return next((name for name, key in LICENCES if key in text), "unknown"), entry
    return "unknown", None


def drop_flag(path: Path) -> bool:
    """Remove `disable-model-invocation` from a SKILL.md's frontmatter, so the model may start it. True if it was there."""
    with open(path, encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0) if lines and lines[0].strip() == "---" else 0
    keep = [line for i, line in enumerate(lines) if not (0 < i < end and line.startswith("disable-model-invocation:"))]
    if len(keep) == len(lines):
        return False
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(keep))
    return True


def _bundle(tree, commit, source, wanted, name, accept_caution, replace, ask, box, out, hooks=False, confirm=None) -> int:
    """Install the skills and agents in `tree` as a plugin folder in the Almanac bundle, after the scan.

    Copied as plain text: no links, no scripts run, no hooks unless `hooks` is set. MCP servers are kept only when pinned to an exact version.
    """
    name = name or re.sub(r"[^A-Za-z0-9._-]", "-", source_name(source)) or "plugin"
    if not NAME.match(name) or name.lower() in ("almanac", "sources.md"):
        out(f"almanac: {name!r} is not usable as a bundle folder name; give one with --name")
        return ERROR
    dest = bundle_dir() / name
    if (dest.exists() or linked(dest)) and not replace:
        out(f"almanac: {dest} already exists; add --replace to overwrite it, or --name to use another folder")
        return ERROR
    try:
        found = find_skills(tree, wanted, name)
    except ValueError as exc:
        out(f"almanac: {exc}")
        return ERROR
    agents = [] if wanted else sorted(p for p in (tree / "agents").glob("*.md") if p.is_file() and not linked(p))
    if not (found or agents):
        out(f"almanac: no skills (SKILL.md) or agents (agents/*.md) in {source}")
        return ERROR
    targets = found if wanted else [(name, tree)]
    hook_dir, kept_hooks = box / "hooks-stage", []
    if hooks:
        hook_dir.mkdir()
        kept_hooks = stage_hooks(tree, hook_dir)
        if kept_hooks and wanted:
            targets = targets + [("hooks", hook_dir)]  # a picked skill's scan does not reach the hooks
    results, stop = _check(targets, accept_caution, ask, out, confirm)
    if stop is not None:
        return stop
    res = max(results, key=code)
    staged = box / "install"
    for skill, folder in found:
        copy(folder, staged / "skills" / skill)
    for agent in agents:
        (staged / "agents").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(agent, staged / "agents" / agent.name)
    if kept_hooks:
        shutil.copytree(hook_dir, staged, dirs_exist_ok=True)
    flagged = [skill for skill, _ in found if drop_flag(staged / "skills" / skill / "SKILL.md")]
    licence, licence_file = find_licence(tree)
    if licence_file:
        shutil.copyfile(licence_file, staged / "LICENSE")
    parts = plugin_parts(tree) or {"hooks": [], "mcp": []}
    servers = {} if wanted else mcp_servers(tree)
    kept = {n: s for n, s in servers.items() if isinstance(s, dict) and isinstance(s.get("command"), str)
            and any(isinstance(a, str) and PINNED.search(a) for a in s.get("args") or [])}
    original = _json(tree / ".claude-plugin" / "plugin.json")
    manifest = {"name": name, "version": str(original.get("version") or "0.0.0"),
                "description": str(original.get("description") or f"{name}, adopted into Almanac.")[:300]}
    if licence != "unknown":
        manifest["license"] = licence
    if kept:
        manifest["mcpServers"] = kept
    (staged / ".claude-plugin").mkdir()
    save_json(staged / ".claude-plugin" / "plugin.json", manifest)
    remove(dest)
    dest.parent.mkdir(exist_ok=True)
    os.replace(staged, dest)
    changed = ["manifest written here"]
    changed += [OVERRIDE_NOTE] if code(res) == VERDICTS["DO_NOT_INSTALL"] else []
    changed += [f"disable-model-invocation removed from {len(flagged)} skill(s)"] if flagged else []
    left = [line for line in parts["hooks"] if line not in kept_hooks]
    changed += [f"hooks kept ({len(kept_hooks)})"] if kept_hooks else []
    changed += [f"hooks not installed ({len(left)})"] if left else []
    changed += [f"MCP server(s) kept, pinned: {', '.join(kept)}"] if kept else []
    changed += [f"MCP server(s) left out, no exact version: {', '.join(n for n in servers if n not in kept)}"] if len(kept) < len(servers) else []
    row = f"| `{name}` | {source} | {(commit or 'local')[:8]} | {licence} | {'; '.join(changed)} |"
    sources = bundle_dir() / "SOURCES.md"
    with open(sources, encoding="utf-8", newline="") if sources.exists() else io.StringIO(BUNDLE_HEAD) as handle:
        text = handle.read()
    eol = "\r\n" if "\r\n" in text else "\n"  # an imported file keeps its own line endings
    lines = [ln for ln in text.splitlines() if not ln.startswith(f"| `{name}` |")] + [row.replace("\n", " ")]
    with open(sources, "w", encoding="utf-8", newline="") as handle:
        handle.write(eol.join(lines) + eol)
    note_scan(dest, "plugin", tree_hash(dest), res, f"bundle/{name}")
    out(f"installed {name}: {len(found)} skill(s), {len(agents)} agent(s)")
    report = integration_report(dest, bundle_dir())
    _mark(name, bool(report))
    for line in report:
        out(f"  {line}")
    if report:
        out(f"  integration pending: {len(report)} open item(s). Resolve them, then run: almanac skill integrate {name}")
    for line in kept_hooks:
        out(f"  hook kept: {line}")
    for line in left:
        out(f"  hook NOT installed (your call): {line}")
    for n in servers:
        out(f"  MCP server {n}: " + ("kept, pinned. It runs code fetched from a registry on first use; the scan covered text only"
                                    if n in kept else "left out, no exact version pinned"))
    out(f"bundle folder: {dest}")
    return code(res)

OVERLAP = 0.5  # share of description words two skills or agents must have in common (the same name always counts)
STOP = frozenset("the and for with use when this that you your from into are any not can will all its our".split())
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)")
ROOT_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./@-]+)")


def _front(path: Path):
    """(name, description) from a SKILL.md or agent file's frontmatter; the name falls back to the folder or file name."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0) if lines[:1] == ["---"] else 0
    meta, key = {}, None
    for line in lines[1:end]:
        if line[:1] in (" ", "\t") and key:
            meta[key] += " " + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            meta[key] = value.strip().strip("\"'>|")
    name = meta.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem)
    return name, meta.get("description", "")


def _items(root: Path):
    """[(kind, path)] for every SKILL.md and every .md file in a folder called agents under `root`."""
    found = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not linked(os.path.join(here, d)))
        if "SKILL.md" in files:
            found.append(("skill", Path(here) / "SKILL.md"))
            dirs[:] = []
        elif os.path.basename(here) == "agents":
            found += [("agent", Path(here) / f) for f in sorted(files) if f.endswith(".md")]
    return found


def _words(text: str):
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP}


def _shown(path: Path, bundle_root: Path) -> str:
    if path.is_relative_to(bundle_root):
        return path.relative_to(bundle_root).as_posix()
    return f"shipped/{path.relative_to(SHIPPED).as_posix()}"


def integration_report(dest, bundle_root) -> list:
    """What still stops the plugin at `dest` from being one coherent part of the bundle. Empty when nothing does.

    Outside references, overlaps with other skills or agents (bundle and shipped), and layout problems.
    Pure and deterministic: the same folders give the same lines.
    """
    dest, bundle_root = Path(dest), Path(bundle_root)
    lines, root = [], os.path.realpath(bundle_root)
    for md in sorted(dest.rglob("*.md")):
        if any(part in SKIP for part in md.relative_to(dest).parts) or linked(md):
            continue
        text = FENCE.sub("", md.read_text(encoding="utf-8", errors="replace"))
        for target in sorted(set(MD_LINK.findall(text))):
            path = target.split("#")[0]
            if not path or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", path) or path.startswith("${"):
                continue
            real = os.path.realpath(md.parent / path)
            if not (os.path.exists(real) and (real == root or real.startswith(root + os.sep))):
                lines.append(f"outside reference: {_shown(md, bundle_root)} links to {target}, which is not in the bundle")
        for rel in sorted(set(ROOT_PATH.findall(text))):
            if not (dest / rel.rstrip(".")).exists():
                lines.append(f"outside reference: {_shown(md, bundle_root)} uses ${{CLAUDE_PLUGIN_ROOT}}/{rel}, which does not exist")
    mine = _items(dest)
    for kind, path in mine:
        rel = path.relative_to(dest).parts
        if not (rel[0] == "skills" if kind == "skill" else len(rel) == 2 and rel[0] == "agents"):
            want = "skills/NAME/SKILL.md" if kind == "skill" else "agents/NAME.md"
            lines.append(f"layout: {_shown(path, bundle_root)} is not where Claude Code loads it ({want})")
    trees = sorted(d for d in bundle_root.iterdir() if d.is_dir() and d.resolve() != dest.resolve()) if bundle_root.is_dir() else []
    others = [item for tree in trees for item in _items(tree)]
    if SHIPPED.is_dir() and SHIPPED.resolve() != bundle_root.resolve():
        others += _items(SHIPPED)
    known = [(path, *_front(path)) for _, path in others]
    for _, path in mine:
        name, desc = _front(path)
        words = _words(desc)
        for other, other_name, other_desc in known:
            both = words | _words(other_desc)
            score = len(words & _words(other_desc)) / len(both) if both else 0
            if name.lower() == other_name.lower() or score >= OVERLAP:
                lines.append(f"overlap: {_shown(path, bundle_root)} and {_shown(other, bundle_root)} "
                             f"do the same job ({name} / {other_name})")
    return sorted(set(lines))


def pending() -> list:
    """Bundle plugins adopted but not yet integrated, that are still installed."""
    marked = load_json(_records_path(), {}).get("integration", {})
    return sorted(n for n in marked if (bundle_dir() / n).is_dir())


def _mark(name, open_) -> None:
    records = load_json(_records_path(), {})
    marks = records.setdefault("integration", {})
    if open_:
        marks[name] = "pending"
    elif not marks.pop(name, None):
        return
    save_json(_records_path(), records)


def integrate(name, out=print) -> int:
    """Re-run the integration report for a bundle plugin. 1 while anything is open; 0 and cleared when clean."""
    dest = bundle_dir() / name
    if not NAME.match(name) or linked(dest) or not dest.is_dir():
        out(f"almanac: no plugin called {name} in {bundle_dir()}")
        return ERROR
    lines = integration_report(dest, bundle_dir())
    for line in lines:
        out(line)
    _mark(name, bool(lines))
    out(f"{name}: {len(lines)} open item(s)" if lines else f"{name}: integrated")
    return 1 if lines else 0


SOURCE_ROW = re.compile(r"\| `([^`]+)` \| (.*?) \| ([^|]*?) \| [^|]* \| (.*) \|\s*\Z")
GITHUB_SOURCE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?: \(`([^`]+)`\))?\Z")
HTTPS_SOURCE = re.compile(r"(https://[\w.:/@%~+-]+)(?: \(`([^`]+)`\))?\Z")


def source_url(source: str):
    """(https git URL, skill path or "") for a SOURCES.md source cell, or None when it is not a git source."""
    if match := GITHUB_SOURCE.match(source):
        return f"https://github.com/{match[1]}/{match[2]}.git", match[3] or ""
    if match := HTTPS_SOURCE.match(source):
        return match[1], match[2] or ""
    return None


def update(name=None, out=print) -> int:
    """Say which adopted skills have a newer upstream commit. Changes nothing: re-adopting stays with the adopt skill."""
    tables = (("user", bundle_dir() / "SOURCES.md"), ("shipped", Path(__file__).parent / "bundle" / "SOURCES.md"))
    newer = unchecked = seen = 0
    for kind, path in tables:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = SOURCE_ROW.match(line)
            if not row or (name and row[1] != name):
                continue
            seen += 1
            folder, source, commit, changed = row[1], row[2], row[3], row[4]
            parsed = source_url(source)
            if not parsed or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
                out(f"{folder}: skipped, not a git source ({source})")
                continue
            url, sub = parsed
            try:
                proc = _git(["ls-remote", "--", url, "HEAD"], timeout=60)
                reason = proc.stderr.strip()[-200:] or "no HEAD found" if proc.returncode or not proc.stdout.strip() else ""
            except (OSError, subprocess.SubprocessError) as exc:
                reason = str(exc)
            if reason:
                unchecked += 1
                out(f"{folder}: could not check ({reason})")
                continue
            head = proc.stdout.split()[0]
            if head.startswith(commit):
                out(f"{folder}: up to date ({commit})")
                continue
            newer += 1
            out(f"{folder}: newer ({commit} -> {head[:8]})")
            if kind == "shipped":
                out("  shipped with Almanac: re-adopt in the Almanac repo and bump SOURCES.md")
            else:
                last = sub.rstrip('/').rsplit('/', 1)[-1]
                # an agents/ folder holds agents, not a SKILL.md folder, so --skill would take nothing
                pick = f" --skill {last}" if sub and last != "agents" else ""
                out(f"  almanac skill add --bundle {url} --name {folder}{pick} --replace")
            out(f"  edits to re-apply after re-adopting (old Changed cell): {changed}")
    if name and not seen:
        out(f"no adopted skill named {name} in a SOURCES.md")
        return ERROR
    return 1 if newer else ERROR if unchecked else 0


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



# The launch check. A scan takes 15 s for a one-file skill and minutes for a plugin, so launch only
# hashes and reads stored results; new or changed items are scanned by a background process, and
# what it finds is shown at the next start.
LAUNCH_SCANS = 5
LOCK_STALE = 3600


def _scans_path():
    return config.home() / "skill-scans.json"


def _lock_path():
    return config.home() / "skill-scan.lock"


def note_scan(path, kind, digest, res, label=""):
    """Remember a verdict for this folder at this hash, so it is not scanned again until it changes."""
    data = load_json(_scans_path(), {})
    data[os.path.realpath(path)] = {"label": label or Path(path).name, "kind": kind, "hash": digest,
                                    "verdict": res["verdict"], "score": res["score"], "issues": res["issues"][:3],
                                    "error": res["error"], "date": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_json(_scans_path(), data)


def installed_items():
    """[(label, kind, real path)] for each folder under <claude config>/skills and each installed plugin."""
    root, items = claude_dir(), []
    if (root / "skills").is_dir():
        for entry in sorted((root / "skills").iterdir()):
            real = os.path.realpath(entry)
            # what Claude Code loads: <name>/SKILL.md, not hidden folders (a trash, a half-done install)
            if os.path.isfile(os.path.join(real, "SKILL.md")) and not entry.name.startswith("."):
                items.append((entry.name, "skill", real))
    mine = bundle_dir()
    for entry in sorted(mine.iterdir()) if mine.is_dir() else []:
        if (entry / ".claude-plugin" / "plugin.json").is_file():
            items.append((f"bundle/{entry.name}", "plugin", os.path.realpath(entry)))
    # the plugin list, not plugins/cache: the cache also holds old versions and half-finished downloads
    plugins = load_json(root / "plugins" / "installed_plugins.json", {}).get("plugins") or {}
    for name, installs in sorted(plugins.items()) if isinstance(plugins, dict) else []:
        for install in installs if isinstance(installs, list) else []:
            path = install.get("installPath") if isinstance(install, dict) else None
            if isinstance(path, str) and os.path.isdir(path):
                items.append((name, "plugin", os.path.realpath(path)))
    return list(dict.fromkeys(items))  # one plugin installed in two scopes points at the same folder


def triage(items, budget=None):
    """(flagged, todo, unhashed): stored non-SAFE results still current, items to scan, items left for time."""
    records, flagged, todo = load_json(_scans_path(), {}), [], []
    deadline = time.monotonic() + budget if budget else None
    for n, (label, kind, path) in enumerate(items):
        digest, record = tree_hash(path, deadline), records.get(path)
        if digest is None:  # out of time, part way through this item or before it
            return flagged, todo, items[n:]
        if isinstance(record, dict) and record.get("hash") == digest:
            if record.get("verdict") != "SAFE" and record.get("accepted") != digest:
                flagged.append((label, path, record))
        else:
            todo.append((label, kind, path, digest))
    return flagged, todo, []


def busy() -> bool:
    try:
        return time.time() - _lock_path().stat().st_mtime < LOCK_STALE
    except OSError:
        return False


def scan_items(todo, out=print) -> int:
    """Scan and record each item, one scan at a time across processes. Returns the worst exit code."""
    lock = _lock_path()
    if not busy():
        lock.unlink(missing_ok=True)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        out("almanac: another skill scan is running; try again when it finishes")
        return ERROR
    worst = 0
    try:
        for label, kind, path, digest in todo:
            res = scan(path)
            out(render(res, f"{label} ({kind})", limit=3))
            if res["error"] == "missing":
                return ERROR
            note_scan(path, kind, digest, res, label)
            worst = max(worst, code(res))
            os.utime(lock)  # still alive
    finally:
        lock.unlink(missing_ok=True)
    return worst


def spawn(limit: int):
    """Start `almanac skill audit --pending N` in the background, detached from this console."""
    logs = config.home() / "logs"
    logs.mkdir(exist_ok=True)
    flags = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}  # no window, own Ctrl+C group
    with open(logs / "skill-scan.log", "ab") as log:
        subprocess.Popen([sys.executable, "-m", "getalmanac", "skill", "audit", "--pending", str(limit)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **flags)


def warning(flagged) -> str:
    lines = ["almanac: WARNING: SkillSpector flagged installed skills or plugins:"]
    for label, path, record in flagged:
        if record.get("verdict") == "ERROR":
            lines.append(f"  {label} ({record.get('kind')}): not scanned, the scan failed: {record.get('error')}")
        else:
            lines.append(f"  {label} ({record.get('kind')}): {record.get('verdict')}, score {record.get('score')}/100")
        for i in record.get("issues") or []:
            lines.append(f"      {i.get('severity')} {i.get('category')}  {i.get('file')}" + (f":{i['line']}" if i.get("line") else ""))
        lines.append(f'      detail: almanac skill scan "{path}"')
    lines.append("  Almanac does not block or remove anything; that is your call.")
    return "\n".join(lines)


def launch_check(cfg, out=None, start=spawn):
    """Warn about flagged items and start a background scan of new or changed ones. Never blocks for long."""
    out = out or (lambda text: print(text, file=sys.stderr))
    if not cfg["skills"].get("scan_on_launch", True):
        return
    flagged, todo, unhashed = triage(installed_items(), budget=10)
    if flagged:
        out(warning(flagged))
    waiting = [t[0] for t in todo] + [u[0] for u in unhashed]
    if not waiting:
        return
    if not scanner():
        out(f"almanac: skill scans are off ({len(waiting)} unscanned); turn them on with: {INSTALL}")
        return
    if busy():
        out(f"almanac: a background skill scan is still running; {len(waiting)} item(s) wait for it.")
        return
    now = min(LAUNCH_SCANS, len(todo))
    if now:
        start(now)
    rest = list(dict.fromkeys(waiting[now:]))  # two versions of one plugin are named once
    out(f"almanac: scanning {now} new or changed skill(s)/plugin(s) in the background; results show at the next start."
        + (f"\n  pending ({len(rest)}): {', '.join(rest[:10])}{' ...' if len(rest) > 10 else ''}; "
           "`almanac skill audit` scans them all now" if rest else ""))


def audit(pending=None, again=False, out=print) -> int:
    """Scan installed items not yet scanned at their current contents (all with `again`), then print a table."""
    items = installed_items()
    _, todo, _ = triage(items)
    if again:
        todo = [(label, kind, path, tree_hash(path)) for label, kind, path in items]
    if pending is not None:
        return scan_items(todo[:pending], out) if todo else 0
    if todo and not scanner():
        out(render(result("ERROR", error="missing"), "skills"))
        return ERROR
    if todo:
        out(f"scanning {len(todo)} item(s); a plugin can take a few minutes")
        scan_items(todo, out)
    records, worst = load_json(_scans_path(), {}), 0
    out(f"\n{'VERDICT':<24}{'SCORE':>5}  {'KIND':<6} NAME")
    for label, kind, path in items:
        record = records.get(path) or {}
        verdict = record.get("verdict", "UNSCANNED")
        worst = max(worst, VERDICTS.get(verdict, ERROR))
        if record.get("accepted") and record.get("accepted") == record.get("hash"):
            verdict = f"accepted {verdict}"
        top = (record.get("issues") or [{}])[0]
        detail = f"  {top.get('severity')} {top.get('category')} {top.get('file')}" if top else ""
        score = record.get("score")
        out(f"{verdict:<24}{'-' if score is None else score:>5}  {kind:<6} {label}{detail}")
    return worst


def import_bundle(path, out=print) -> int:
    """Copy the plugin folders in `path` (a bundle kept elsewhere) into the user bundle, keeping any that are already there."""
    src, dest = Path(path), bundle_dir()
    found = sorted(p for p in src.iterdir() if (p / ".claude-plugin" / "plugin.json").is_file()) if src.is_dir() else []
    if not found:
        out(f"almanac: no plugin folders (each with .claude-plugin/plugin.json) in {path}")
        return ERROR
    dest.mkdir(exist_ok=True)
    for folder in found:
        if folder.name == "almanac":  # the package carries its own, newer one
            out("skipped almanac: the package already carries it")
        elif (dest / folder.name).exists() or linked(dest / folder.name):
            out(f"kept {folder.name}: already in {dest}")
        else:
            copy(folder, dest / folder.name)
            out(f"imported {folder.name}")
    for name, target in (("SOURCES.md", dest / "SOURCES.md"), ("pack.json", config.home() / "pack.json")):
        if (src / name).is_file() and not target.exists():  # the notes and the pack come along, never over newer ones
            shutil.copyfile(src / name, target)
    return 0


def _find(which):
    """(label, kind, path) of the installed item with this name or path, or None."""
    real = os.path.realpath(which) if os.path.exists(which) else None
    for item in installed_items():
        if which in (item[0], item[0].split("@")[0]) or item[2] == real:
            return item
    return None


def accept(which, out=print) -> int:
    """Record that the owner has read this item's findings at its current contents. Only the launch warning changes."""
    item = _find(which)
    if not item:
        out(f"almanac: no installed skill or plugin called {which!r}")
        return 2
    label, kind, path = item
    data = load_json(_scans_path(), {})
    record = data.get(path)
    if not isinstance(record, dict):
        out(f"almanac: {label} has no stored scan result; run `almanac skill audit` first")
        return 2
    if record.get("hash") != tree_hash(path):
        out(f"almanac: {label} changed since its last scan; run `almanac skill audit` and look again")
        return 2
    out(warning([(label, path, record)]).split("\n", 1)[1].rsplit("\n", 1)[0])
    record["accepted"] = record["hash"]
    save_json(_scans_path(), data)
    out(f"accepted {label} at this exact content. Any change to its files brings the warning back.")
    return 0


def unaccept(which, out=print) -> int:
    item = _find(which)
    data = load_json(_scans_path(), {})
    record = data.get(item[2]) if item else None
    if not isinstance(record, dict) or "accepted" not in record:
        out(f"almanac: {which!r} is not accepted")
        return 2
    del record["accepted"]
    save_json(_scans_path(), data)
    out(f"{item[0]} warns at launch again")
    return 0


# The pack: your own list of third-party skills and plugins, each pinned to a commit.
KINDS = ("skill", "plugin", "binary", "builtin")


def pack_file(path=None):
    """pack.json: the given path, else the one in the Almanac folder (the package ships none)."""
    return Path(path) if path else config.home() / "pack.json"


def load_pack(path=None):
    data = load_json(pack_file(path), {})
    entries = [e for e in data.get("entries") or [] if isinstance(e, dict)]
    for entry in entries:
        if entry.get("kind") not in KINDS or not isinstance(entry.get("name"), str):
            raise ValueError(f"pack entry {entry.get('name')!r}: kind must be one of {', '.join(KINDS)}")
    return entries


def pack_install(entries, accept_caution=False, replace=False, out=print) -> int:
    """Skills go through `add` pinned to their ref; plugins are scanned and shown; binaries are listed."""
    worst = 0
    for entry in entries:
        out(f"\n== {entry['name']} ({entry['kind']}): {entry.get('note', '')}")
        if entry["kind"] in ("skill", "plugin"):
            rc = add(entry["source"], entry.get("skills"), entry.get("ref"), accept_caution, replace,
                     plugin=entry["kind"] == "plugin", out=out)
        else:
            out(f"Not a skill, so there is nothing for the scanner to check. Install it by hand: {entry.get('source')}")
            rc = 0
        worst = max(worst, rc)
    return worst
