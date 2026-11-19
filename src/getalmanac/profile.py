"""The clean `almanac` profile: Claude Code is started with the user's own setup minus what `profile` switches off.

Hooks cannot be removed by a settings layer on top (hook lists from every source are added together), and
`--setting-sources` drops a whole source. So the launcher drops the user source and rebuilds it:

- `--setting-sources project,local --settings <ALMANAC_HOME>/profile/<account>/settings.json`: the user's settings.json
  with the off plugins set to false and the hooks that match `off_hooks` left out;
- the user's skills (minus `off_skills`), agents and commands come back through `--add-dir <ALMANAC_HOME>/profile/<account>`,
  which holds links to them (a copy where no link can be made);
- the user's CLAUDE.md (with its @imports, minus any section about an off plugin) is written as `<ALMANAC_HOME>/profile/<account>/CLAUDE.md`,
  which Claude Code reads from the added folder (`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`, set in the settings below),
  and `claudeMdExcludes` stops it reading the unfiltered original. A CLAUDE.md reaches sub-agents too; a system prompt
  addition (`--append-system-prompt-file`) reaches the main thread only. The same setting also loads a CLAUDE.md from any
  folder the user adds with `--add-dir` themselves.

Login, plugins that stay on, MCP servers and history are not settings sources and are untouched.
Hooks written into a repository's own `.claude/settings*.json` stay (see `leaks`).
"""
import filecmp
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from . import config

MD_FROM_ADDED_DIR = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"  # makes Claude Code read CLAUDE.md of an --add-dir folder
UNSAFE_FLAGS = ("--setting-sources", "--settings", "--bare", "--restricted")  # the user chose their own setup


def user_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def folder() -> Path:
    """This account's profile folder. Accounts (config folders) run at the same time, so each one has its own."""
    key = os.path.normcase(str(user_dir().resolve()))
    return config.home() / "profile" / hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def off_names(cfg) -> list:
    """Short names of everything switched off: plugin names (`claude-mem`) and skill names."""
    p = cfg["profile"]
    return sorted({x.split("@")[0] for x in p["off_plugins"]} | set(p["off_skills"]))  # the one list: adding a plugin there covers all of the profile


def is_off_hook(hook, cfg) -> bool:
    command = (hook.get("command", "") if isinstance(hook, dict) else "").lower()
    return any(pattern.lower() in command for pattern in [*cfg["profile"]["off_hooks"], *off_names(cfg)])


def strip_hooks(hooks, cfg):
    """`hooks` of a settings file without the commands that match `off_hooks`; groups and events left empty go too."""
    out = {}
    for event, groups in (hooks or {}).items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            inner = [h for h in group.get("hooks", []) if not is_off_hook(h, cfg)] if isinstance(group, dict) else []
            if inner:
                kept.append({**group, "hooks": inner})
        if kept:
            out[event] = kept
    return out


def curated_settings(user: dict, cfg, md_path: str = "") -> dict:
    """The user's settings with the off plugins, hooks and skills removed. Pure."""
    out = json.loads(json.dumps(user))
    p = cfg["profile"]
    out["enabledPlugins"] = {**out.get("enabledPlugins", {}), **{name: False for name in p["off_plugins"]}}  # false beats a project's true
    out["skillOverrides"] = {**out.get("skillOverrides", {}), **{name: "off" for name in p["off_skills"]}}
    if "hooks" in out:
        out["hooks"] = strip_hooks(out["hooks"], cfg)
    if md_path:  # the filtered CLAUDE.md is read from the profile folder instead: do not read the original too
        out["claudeMdExcludes"] = [*out.get("claudeMdExcludes", []), md_path]
        out["env"] = {**out.get("env", {}), MD_FROM_ADDED_DIR: "1"}
    return out


def drop_sections(text: str, names) -> str:
    """Markdown without the sections whose heading names one of `names` (up to the next heading of the same or a higher level)."""
    out, skip = [], 0
    for line in text.splitlines():
        if re.match(r"\s*<!--.*-->\s*$", line) and any(n.lower() in line.lower() for n in names):
            continue  # a marker such as <!-- logbook:start -->
        head = re.match(r"(#+)\s+(.*)", line)
        if head:
            level = len(head.group(1))
            if skip and level <= skip:
                skip = 0
            if not skip and any(n.lower() in head.group(2).lower() for n in names):
                skip = level
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def claude_md(folder: Path, cfg) -> str:
    """The user's CLAUDE.md with each `@file` line replaced by that file's text, minus the off plugins' sections."""
    try:
        text = (folder / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return ""

    def include(match):
        try:
            return (folder / match.group(1)).read_text(encoding="utf-8")
        except OSError:
            return match.group(0)

    text = re.sub(r"(?m)^@(\S+)\s*$", include, text)
    return drop_sections(text, off_names(cfg))


def _remove(path: Path):
    if path.is_symlink():
        path.unlink()
        return
    try:
        os.rmdir(path)  # a junction is removed, never its target; a real folder with files fails
    except OSError:
        shutil.rmtree(path) if path.is_dir() else path.unlink()


def _link(src: Path, dst: Path):
    """A link to a folder where one can be made (a junction on Windows needs no rights), else a copy."""
    if src.is_dir():
        try:
            os.symlink(src, dst, target_is_directory=True)
            return
        except OSError:
            pass
        if os.name == "nt":
            try:
                import _winapi

                _winapi.CreateJunction(str(src.resolve()), str(dst))
                return
            except OSError:
                pass
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _tmp(path: Path) -> Path:
    """A temp name of this process: two launches at once never write the same one."""
    return path.with_name(f"{path.name}.{os.getpid()}.tmp")


def _put(path: Path, text: str):
    """Write `text` to `path` only when it differs, and by replacing the file whole: a running session never reads half of it."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    tmp = _tmp(path)
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        try:
            if path.read_text(encoding="utf-8") == text:  # another launch won the race with the same text
                return
        except OSError:
            pass
        raise


def _same_tree(a: Path, b: Path) -> bool:
    d = filecmp.dircmp(a, b)
    return not (d.left_only or d.right_only or d.diff_files or d.funny_files) and all(_same_tree(a / n, b / n) for n in d.common_dirs)


def _current(src: Path, dst: Path) -> bool:
    """True when `dst` already is what `_link(src, dst)` makes: a link to the folder, a copy of it, or a file with the same bytes."""
    try:
        if src.is_dir():
            if dst.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(dst):
                return os.path.samefile(src, dst)
            return dst.is_dir() and _same_tree(src, dst)  # the copy made where no link can be made
        return dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False)
    except OSError:  # a link whose target is gone
        return False


def _swap(src: Path, dst: Path):
    """Put a fresh link or copy of `src` where `dst` is, under a temp name first, so `dst` is missing only for one rename.

    shortcut: the rename is not atomic with the removal of the old `dst`; a reader can miss it for that instant."""
    tmp = _tmp(dst)
    if os.path.lexists(tmp):
        _remove(tmp)
    if src.is_dir():
        _link(src, tmp)
        _remove(dst)
        os.replace(tmp, dst)
    else:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)


def mirror(source: Path, dest: Path, cfg):
    """Make `dest/.claude/{skills,agents,commands}` hold links to the user's, without the off skills.

    Only what differs is touched, so a session that is running from this folder never finds it emptied."""
    skip = set(cfg["profile"]["off_skills"])
    for kind in ("skills", "agents", "commands"):
        folder = dest / ".claude" / kind
        folder.mkdir(parents=True, exist_ok=True)
        want = {}
        if (source / kind).is_dir():
            for item in sorted((source / kind).iterdir()):
                stem = item.name[:-3] if item.name.endswith(".md") else item.name
                if not (item.name.startswith(".") or (kind == "skills" and stem in skip)):
                    want[item.name] = item
        for old in folder.iterdir():
            if old.name not in want and not old.name.endswith(".tmp"):  # a .tmp is another launch at work
                _remove(old)
        for name, item in want.items():
            target = folder / name
            if os.path.lexists(target):
                if not _current(item, target):
                    _swap(item, target)
                continue
            try:
                _link(item, target)
            except FileExistsError:  # another launch linked it between our check and our link
                if not _current(item, target):
                    raise


def leaks(cwd, cfg) -> list:
    """Repository settings files that would still run an off hook or turn an off plugin on."""
    found = []
    for name in ("settings.json", "settings.local.json"):
        path = Path(cwd) / ".claude" / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hooks = [h for groups in (data.get("hooks") or {}).values() if isinstance(groups, list)
                 for g in groups if isinstance(g, dict) for h in g.get("hooks", []) if is_off_hook(h, cfg)]
        if hooks:
            found.append(str(path))
    return found


def prepare(cfg, args, cwd=None):
    """(Claude Code arguments to put first, notes to print). Writes the files under ALMANAC_HOME/profile/<account>."""
    if not cfg["profile"]["enabled"]:
        return [], []
    if any(a == f or a.startswith(f + "=") for a in args for f in UNSAFE_FLAGS):
        return [], ["the clean profile is off for this run: the command line sets its own settings"]
    source = user_dir()
    try:
        user = json.loads((source / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        user = {}
    except (OSError, ValueError) as exc:
        return [], [f"the clean profile is off for this run: {source / 'settings.json'} was not read ({exc})"]
    mine = folder()
    mine.mkdir(parents=True, exist_ok=True)
    text = claude_md(source, cfg)
    md = source / "CLAUDE.md"
    if text:
        _put(mine / "CLAUDE.md", text)
    else:
        (mine / "CLAUDE.md").unlink(missing_ok=True)  # an old copy must not outlive the user's file
    _put(mine / "settings.json", json.dumps(curated_settings(user, cfg, md.as_posix() if text else ""), indent=2))
    mirror(source, mine, cfg)
    # --add-dir takes every value up to the next flag, so it must not be the last one before the user's own arguments
    flags = ["--add-dir", str(mine), "--setting-sources", "project,local", "--settings", str(mine / "settings.json")]
    notes = [f"{path} runs a hook this profile leaves out; it will still run here" for path in leaks(cwd or os.getcwd(), cfg)]
    return flags, notes
