"""Where Almanac keeps its files, and its settings."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "upstream": "https://api.anthropic.com",
    "port": 8787,  # where `almanac serve` listens, and where `almanac` looks for one to share
    # true runs sessions in HTTPS-proxy mode, which keeps Remote Control available (see tls.py)
    "remote_control": False,
    # a corporate proxy to reach the upstream through: http://host:port, or http://user:pass@host:port.
    # "" uses HTTPS_PROXY / ALL_PROXY from the environment (NO_PROXY is honoured either way)
    "upstream_proxy": "",
    "upstream_ca": "",  # a CA file to trust for the upstream, for a proxy that re-signs TLS
    "offline": {"hold_minutes": 480},  # how long a model request waits for the network to return; 0 fails at once
    "accounts": {
        # Claude Code config folders to carry on with when the login in use is out of usage,
        # for example ["~/.claude-work", "~/.claude"]. Empty turns this off (see accounts.py)
        "fallback": [],
        "cooldown_minutes": 30,  # how long a login that ran out of usage is left alone before it is tried again
    },
    "skills": {
        "enabled": True,
        "stub_chars": 60,  # characters of description kept for a collapsed skill, cut at a word; 0 lists names only (the full text shows when a message matches)
        "max_full": 6,  # most skills shown in full per message on a match
        "always_full": [],  # skill names never collapsed
        "scan_on_launch": True,  # scan new or changed skills and plugins with SkillSpector in the background (skills.py)
    },
    # What `almanac` loads Claude Code with: the user's own settings minus what is off here (see profile.py)
    "profile": {
        "enabled": True,
        "off_plugins": ["claude-mem@thedotmack", "logbook@logbook", "pocketbook@pocketbook"],  # plugin ids switched off
        "off_skills": ["logbook"],  # user skills switched off
        # A user-settings hook is left out when its command contains one of these, or the name of anything in the two
        # lists above (the part of a plugin id before the @), so a new plugin under development is one line in off_plugins.
        "off_hooks": ["getlogbook"],
        # Hook text of a plugin that is not off but whose job Almanac now does itself (a mode banner and its rules) is cut
        # from requests when its first line contains one of these (the banner's own name, such as "ZED MODE": never a
        # generic word, which would cut your own hooks too). The plugin stays installed. Empty: nothing is cut this way.
        "off_hook_text": [],
    },
    "startup": {
        "agent_chars": 160,  # characters of description kept for a listed agent type (enough to tell when to use it); 0 leaves the agent listing alone
        "force_agents": True,  # the sub-agent tool accepts only the listed specialist agent types (startup.ForceAgentsLayer)
        "force_agents_skip": ["general-purpose", "claude"],  # agent types it may not pick: the generic ones
        "lean_rules": True,  # Almanac's own minimal-change rules, added to the main thread and every sub-agent (cheatsheet.py)
        "lean_level": "full",  # lite: build what was asked and name the smaller option; full; ultra: also question the request
        "lean_rules_text": "",  # your own wording instead of the packaged rules ("" keeps them)
        "sub_skill_chars": 0,  # description characters kept per listed skill in a sub-agent thread (0: names only; the full text shows on a match)
        "sub_cold": ["Agent", "Task"],  # tools also deferred in sub-agent threads (none of 5,000 logged sub-agent requests spawned one)
        "sub_ttl": "1h",  # cache life of a sub-agent's tools and system prompt: "1h" costs 2x to write instead of 1.25x but survives a pause; "" keeps the client's 5 minutes
        "system_prefix": False,  # move the stable startup sections to the end of the system prompt so a new conversation can read them from cache (sysprefix.py)
    },
    "tools": {
        "enabled": True,
        # deferred unless the conversation or the project's history calls for them
        "cold": ["Artifact", "ListAgents", "PowerShell", "ReportFindings", "ScheduleWakeup", "SendFeedback"],
        "hot": [],  # tool names never deferred
        "promote_after": 2,  # conversations in the last 30 (of the same kind: main, or one sub-agent type) that used a cold tool before it stays loaded
    },
    "memory": {
        "enabled": True,
        "recall": True,
        "recall_chars": 2400,  # most characters of memory added to a new conversation
        "sub_recall_chars": 800,  # the same for a sub-agent's first message
        "tools": True,  # give Claude Code the memory_search / memory_read tools
        "keep_days": 365,  # conversations untouched for longer are deleted; 0 keeps everything
        "summary_model": "claude-haiku-5-5",  # writes each exchange's title and summary in the background; "" keeps rule-written text
        "summary_daily_calls": 300,  # most summary calls per day
        "always_load": False,  # list the memory tools in every session start instead of behind ToolSearch (adds their schemas, about 1,000 tokens)
        "auto_import": False,  # off: importing is manual (`almanac import`); on: while the proxy runs, import new Claude Code transcripts of stored projects, once an hour, then summarize them and make their vectors (autoimport.py)
        "auto_import_days": 14,  # transcripts older than this are not imported
        "review_gate": True,  # an Opus main thread may not end its turn while its sub-agents' work is unreviewed (a Stop hook; at most 2 holds per conversation)
        "banner": True,  # show the person a short memory summary when a session starts (a SessionStart hook; never enters the prompt)
        "semantic": True,  # search by meaning when the optional packages and model are installed (see semantic.py)
    },
    "library": {
        "enabled": True,  # false: the prompt library tells Claude nothing (ALMANAC_DISABLE=library)
        "min_tools": 5,  # every exchange with a tool call leaves an entry in the prompt library (no model call; deleted with its conversation); searches and lists show those with at least this many calls
        "inject": True,  # tell Claude how an earlier, matching prompt was solved, when no playbook matches (see library.py)
        "threshold": 0.6,  # share (0 to 1) of a stored prompt's words the typed text must contain before that note is shown
        "note_chars": 800,  # most characters of that note
    },
    "compaction": {
        "enabled": True,
        "trigger_tokens": 150000,  # a conversation this large is compacted at its next message
        "target_tokens": 40000,  # most tokens the summary that replaces it may take
        "outline_share": 0.35,  # share of target_tokens the outline generations may take together; past it the oldest are merged into one
        "on_cold": True,  # a request that returns after the cache has expired is compacted from 1.5 times target_tokens, since the prefix is rewritten anyway
        "window_share": 0.85,  # inside one long turn, old tool results are removed from this share of the model's window; 0 turns it off
        "stub_tokens": 120000,  # inside one long turn or a sub-agent, old tool results are removed past this many tokens too; 0: only window_share
        "agent_stub_tokens": 100000,  # a sub-agent is outlined past this many tokens (stub_tokens when 0); each outline rewrites its cache once
        "sub_stub_tokens": 40000,  # a sub-agent's old tool results are removed past this many tokens (below agent_stub_tokens, so stubs come first); 0: stub_tokens
        "sub_budget_tokens": 100000,  # hard limit: past this, a sub-agent is told to stop now and hand back what is done and left; 0: off
        "agent_outline": True,  # past stub_tokens, a sub-agent's older steps become a stored outline (results kept in memory)
        "keep_results": 12,  # tool results of the current turn that are never removed (the newest ones)
        "own_the_context": True,  # also switch off Claude Code's own auto-compaction (DISABLE_AUTO_COMPACT=1)
    },
    "routing": {
        "mode": "off",  # "off" changes nothing; "subagents" routes sub-agent threads; "all" routes the main thread too
        "models": {"small": "claude-haiku-5-5", "medium": "claude-sonnet-5-5", "large": "claude-opus-5-5"},
        "route_inherited": False,  # false: a sub-agent never goes below the model it starts on, even one it only inherited from its parent
        "ceiling": "large",  # the highest tier routing may pick (a pin ignores it)
        "classifier": "rules",  # "model" lets a small model settle the turns the rules are unsure about
        "effort": {"small": "medium", "medium": "medium", "large": "low"},  # a tier's normal effort: set only where Claude Code already sends one; "" keeps its value
        # A thread's model is chosen on its first request and never changes. A failure raises effort to "high" in a
        # last message instead, until calm_after clean requests in a row.
        "escalate_after": 3,  # tool errors in a row, or one call failing this often, raise the thread's effort
        "calm_after": 2,  # requests in a row with clean tool results before a raised effort drops back (0: never)
        "windows": {"claude-haiku-5-5": 1000000, "claude-sonnet-5-5": 1000000, "claude-opus-5-5": 1000000},  # tokens; unknown ids count as 200000
    },
    # "on" appends the working rules for the answering model (Opus, Sonnet or Haiku) to each thread's system prompt
    "bundle": {
        "enabled": True,  # load the package's own plugin and the plugins in <almanac home>/bundle into every session
    },
    "guard": {
        "enabled": True,  # load the guard plugin: block edits to lint and test configs, and commands that skip git hooks or signing
    },
    "cheatsheet": {"mode": "on"},
    # lessons per sub-agent type, distilled from memory_review verdicts by memory.summary_model, added to that type's threads
    "lessons": {"enabled": True},
    "playbooks": {
        "enabled": True,
        "inject": True,  # tell Claude which playbook matches a request (see playbook_layer.py and the getplaybook package)
        "route": True,  # offer a playbook from what a thread is doing (its tool calls and narration), main thread and sub-agents
        "default_mode": "enforce",  # the mode `almanac playbook approve` gives a playbook without --mode: suggest or enforce
        "auto_run": True,  # run a playbook's approved script in the proxy when `approve --auto` allowed it; false never runs one
        # Claude Code config folders to mine for repeated tasks, besides ~/.claude and $CLAUDE_CONFIG_DIR (see the getplaybook package),
        # for example ["~/.claude-work"]
        "transcript_dirs": [],
        # parts of session folder names the miner leaves out (scratch and temporary work, its own compile sessions)
        "skip_folders": ["AppData-Local-Temp", "-Temp-", "almanac-compile", "playbook-compile", "claude-mem-observer"],
        "background": True,  # mine past sessions and compile the best repeat into a playbook draft, in the background
        "nudge_after": 25,  # a turn of this many tool calls, with no playbook saved or matched, is flagged to the next request; 0 is off
        "compile_per_day": 1,  # most Sonnet sessions a day that do the compiling; 0 turns it off
    },
    # Opt-in prompt-cache keepalive (keepalive.py): while a conversation is paused, a tiny request that reads its
    # cached prefix restarts the cache's life. Costs about a tenth of the prefix per ping, against a full rewrite on return.
    "keepalive": {
        "enabled": False,
        "max_idle_minutes": 240,  # stop pinging a conversation this long after its last real request
        "max_lanes": 4,  # conversation threads kept alive at once (the least recently active is dropped first)
    },
    "code": {
        "sync_on_start": True,  # at proxy start, tie recorded edits to git commits when the folder is a git repository
    },
    "cloud": {
        "auto": False,  # true keeps a running task's snapshot on origin's cloud/handoff branch (see cloud.py)
        "heartbeat_minutes": 10,
        "stall_after": 5,  # push at once, marked "stalled", after this many failed upstream attempts in a row; 0 is off
        "repos": [],  # project keys of the repositories that take part; `almanac cloud on` / `off` in one changes it
        "allow_public": False,  # true pushes the (sealed) hand-off branch even when `gh` says the repository is public
        "routine": "",  # id of the scheduled cloud task (routine) that takes over; enabled only while a hand-off is live and its login has usage left (UNVERIFIED API, see cloud.ROUTINE_*)
    },
}


# ALMANAC_DISABLE name -> the (section, key, off value) settings that switch it off
_OFF = {
    "skills": [("skills", "enabled", False)],
    "agents": [("startup", "agent_chars", 0), ("startup", "force_agents", False)],
    "tools": [("tools", "enabled", False)],
    "memory": [("memory", "enabled", False)],
    "compaction": [("compaction", "enabled", False)],
    "playbooks": [("playbooks", "enabled", False)],
    "library": [("library", "enabled", False)],
    "profile": [("profile", "enabled", False)],
    "bundle": [("bundle", "enabled", False)],
    "guard": [("guard", "enabled", False)],
    "routing": [("routing", "mode", "off")],
    "cheatsheet": [("cheatsheet", "mode", "off")],
    "lessons": [("lessons", "enabled", False)],
    "cloud": [("cloud", "auto", False)],
    "keepalive": [("keepalive", "enabled", False)],
    "accounts": [("accounts", "fallback", [])],
    "startup": [("startup", "agent_chars", 0), ("startup", "force_agents", False), ("startup", "lean_rules", False), ("startup", "system_prefix", False)],
}

POSIX = os.name != "nt"


def private(path, mode: int):
    """Limit a file or folder to this user. Does nothing on Windows, where the profile folder is already private."""
    if POSIX:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def home() -> Path:
    path = Path(os.environ.get("ALMANAC_HOME") or Path.home() / ".almanac")
    path.mkdir(parents=True, exist_ok=True)
    private(path, 0o700)  # holds the stored conversations, so no other user may list it
    return path


def config_path() -> Path:
    return home() / "config.json"


def _merge(base, over):
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _file() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _routing_ok(cfg) -> dict:
    """Put back the default of any routing table that is malformed: a typo must not break every request or send a bad effort."""
    tiers, levels = ("small", "medium", "large"), ("low", "medium", "high", "xhigh", "max")
    route = cfg.get("routing")
    if not isinstance(route, dict):
        cfg["routing"] = copy.deepcopy(DEFAULTS["routing"])
        return cfg
    effort = route.get("effort")
    if not (isinstance(effort, dict) and all(k in tiers and v in ("", *levels) for k, v in effort.items())):
        route["effort"] = copy.deepcopy(DEFAULTS["routing"]["effort"])
    return cfg


def load() -> dict:
    cfg = _routing_ok(_merge(copy.deepcopy(DEFAULTS), _file()))
    if os.environ.get("ALMANAC_UPSTREAM"):
        cfg["upstream"] = os.environ["ALMANAC_UPSTREAM"]
    # ALMANAC_DISABLE=skills,agents,... turns those off for one run; "all" is every name in _OFF
    names = list(filter(None, (os.environ.get("ALMANAC_DISABLE") or "").replace(" ", "").split(",")))
    for name in _OFF if "all" in names else names:
        for section, key, off in _OFF.get(name, ()):
            cfg[section][key] = copy.deepcopy(off)
    return cfg


def set_value(dotted: str, raw: str) -> object:
    """Set `a.b` in config.json. The value is JSON when it parses, else a string."""
    node = DEFAULTS
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    try:
        value = json.loads(raw)
    except ValueError:
        value = {"on": True, "off": False}.get(raw.lower(), raw) if isinstance(node, bool) else raw
    if type(value) is not type(node) and not (isinstance(node, (int, float)) and isinstance(value, (int, float))):
        raise TypeError(f"{dotted} expects {type(node).__name__}")
    data = _file()
    target = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    config_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return value
