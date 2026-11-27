"""Command hook for plan mode: run the EGM layers in order on the plan, and hand approved plans to the planner."""
import json
import os
import re
import sys
import tempfile
import time

NUDGE = ("Plan mode EGM (egm-review): layers run in order zoom-out, implementation-readiness, design, codebase-details. "
         "One layer per owner message, never two at once, never pass a model. "
         "Show findings in chat with a proposed answer to each, and rewrite the plan file. "
         "Use egm-brainstorm or egm-draft first only if the idea has no shape yet.")
LAYERS = ("egm-zoom-out", "egm-implementation-readiness", "egm-design", "egm-codebase-details")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")


def plans_dir():
    return os.path.join(os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir(), "egm-plans")


def handoff(path):
    return (f"The plan was approved. Before any edit: load the egm-handoff skill and write the plan brief to {path}. "
            "Then send that path, and any concepts_brief.md or design doc written in plan mode, to the almanac:planner agent. "
            "Show the person the planner's checklist; pin it with memory_pin_add only after they approve it.")


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _sid(event):
    sid = event.get("session_id")
    return sid if isinstance(sid, str) and SESSION_ID.fullmatch(sid) else None


def _bare(kind):
    return kind[4:] if kind.startswith("egm:") else kind


def _agent(event):
    inp = event.get("tool_input")
    if not isinstance(inp, dict) or not isinstance(inp.get("subagent_type"), str):
        return None
    return _bare(inp["subagent_type"])


def _chain_path(sid):
    return os.path.join(plans_dir(), f"egm_chain-{sid}.json")


def _fresh():
    return {"turn": 0, "layers_done": [], "closed": False, "plan_mode_left": False}


def _load(sid):
    try:
        with open(_chain_path(sid), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if (not isinstance(state, dict) or not isinstance(state.get("turn"), int)
            or not isinstance(state.get("layers_done"), list)
            or not all(isinstance(x, str) for x in state["layers_done"])
            or not isinstance(state.get("closed"), bool) or not isinstance(state.get("plan_mode_left"), bool)):
        return None
    return state


def _save(sid, state):
    os.makedirs(plans_dir(), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=plans_dir())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _chain_path(sid))


def _prune():
    """Delete chain and claim files untouched for 7 days."""
    cutoff = time.time() - 7 * 86400
    try:
        names = os.listdir(plans_dir())
    except OSError:
        return
    for name in names:
        path = os.path.join(plans_dir(), name)
        try:
            if name.startswith(("egm_chain-", "egm_claim-")) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _forget(sid):
    """Delete the chain state and claim files of a plan that was approved."""
    try:
        names = os.listdir(plans_dir())
    except OSError:
        return
    for name in names:
        if name == f"egm_chain-{sid}.json" or name.startswith(f"egm_claim-{sid}-"):
            try:
                os.remove(os.path.join(plans_dir(), name))
            except OSError:
                pass


def _chain_passed(event):
    sid = _sid(event)
    state = _load(sid) if sid else None
    return state is not None and (state["closed"] or bool(state["layers_done"]))


def _lines(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _transcript(event):
    path = event.get("transcript_path")
    if not isinstance(path, str):
        return None
    try:
        return _lines(path)
    except OSError:
        return None  # unreadable transcript: fail open


def _content(line):
    message = line.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _is_prompt(line):
    if line.get("type") != "user" or line.get("isMeta"):  # injected notices are not the person's prompt
        return False
    content = _content(line)
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        not (isinstance(b, dict) and b.get("type") == "tool_result") for b in content)


def _is_egm(line):
    content = _content(line)
    if line.get("type") != "assistant" or not isinstance(content, list):
        return False
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        if b.get("name") == "Skill" and inp.get("skill") in ("egm:egm-brainstorm", "egm:egm-draft"):
            return True
        if (b.get("name") == "Agent" and isinstance(inp.get("subagent_type"), str)
                and _bare(inp["subagent_type"]) in LAYERS):
            return True
    return False


def _gate(event, sid, agent):
    """Deny an EGM layer that has a model, runs out of order, or is a second run in one owner message."""
    if event["tool_input"].get("model") not in (None, ""):
        return _deny("EGM: no model override; drop the model parameter")
    if agent not in LAYERS:
        return _deny("EGM: only the 4 layers run in plan mode (zoom-out, implementation-readiness, design, codebase-details)")
    if sid is None:
        return None  # fail open without a session id
    state = _load(sid) or _fresh()
    if state["closed"]:
        return _deny("EGM: review closed until the plan is approved")
    done = state["layers_done"]
    nxt = next((layer for layer in LAYERS if layer not in done), None)
    if agent != nxt and not (done and agent == done[-1]):
        return _deny(f"EGM: run layers in order; next is {nxt}")
    os.makedirs(plans_dir(), exist_ok=True)
    try:
        fd = os.open(os.path.join(plans_dir(), f"egm_claim-{sid}-{state['turn']}"),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return _deny("EGM: one layer per owner message; wait for the owner")
    os.close(fd)
    return None


def respond(event):
    """The hook output dict for this event, or None when there is nothing to add."""
    name = event.get("hook_event_name")
    sid = _sid(event)
    left = "permission_mode" in event and event["permission_mode"] != "plan"
    if sid and name in ("UserPromptSubmit", "Stop") and left:  # leaving plan mode ends the chain's reuse
        state = _load(sid)
        if state is not None:
            state["plan_mode_left"] = True
            _save(sid, state)
    if name == "UserPromptSubmit" and event.get("permission_mode") == "plan":
        nudge = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": NUDGE}}
        if sid is None:
            return nudge
        state = _load(sid)
        if state is None or state["plan_mode_left"]:
            state = _fresh()
            _prune()
        prompt = str(event.get("prompt", "")).strip().lower()
        if prompt.startswith("<task-notification>"):
            return None  # a background agent's report, not the owner
        if prompt in ("done", "skip"):
            state["closed"] = True
        elif not state["closed"]:
            state["turn"] += 1
            _save(sid, state)
            return nudge
        _save(sid, state)
        return None
    if name == "PreToolUse" and event.get("tool_name") == "Agent" and event.get("permission_mode") == "plan":
        agent = _agent(event) or ""
        if agent.startswith("egm-"):
            try:
                return _gate(event, sid, agent)
            except Exception:
                return _deny("EGM state unreadable")
    if name == "PostToolUse" and event.get("tool_name") == "Agent" and sid and event.get("permission_mode") == "plan" and _agent(event) in LAYERS:
        agent = _agent(event)
        state = _load(sid) or _fresh()
        response = event.get("tool_response")
        if isinstance(response, dict) and response.get("status") in ("completed", "async_launched"):
            if agent not in state["layers_done"]:
                state["layers_done"].append(agent)
            _save(sid, state)
        else:
            try:
                os.remove(os.path.join(plans_dir(), f"egm_claim-{sid}-{state['turn']}"))
            except OSError:
                pass
        return None
    if name == "PostToolUse" and event.get("tool_name") == "ExitPlanMode":
        if sid is None:
            return None  # the id goes into a path, so only plain ids are allowed
        _forget(sid)
        os.makedirs(plans_dir(), exist_ok=True)
        path = os.path.join(plans_dir(), f"{sid}.md")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": handoff(path)}}
    if name == "Stop" and event.get("permission_mode") == "plan" and event.get("stop_hook_active") is not True:
        if _chain_passed(event):
            return None
        lines = _transcript(event)
        if lines is None:
            return None
        last = max((i for i, line in enumerate(lines) if _is_prompt(line)), default=-1)
        if any(_is_egm(line) for line in lines[last + 1:]):
            return None
        return {"decision": "block", "reason": "Plan mode: run EGM layer 1 (egm-zoom-out)"}
    if name == "PreToolUse" and event.get("tool_name") == "ExitPlanMode":
        if _chain_passed(event):
            return None
        lines = _transcript(event)
        if lines is None or any(_is_egm(line) for line in lines):
            return None
        return _deny("Run EGM on the plan before leaving plan mode.")
    return None


def main():
    try:
        out = respond(json.loads(sys.stdin.buffer.read().decode("utf-8")))
    except Exception:  # a broken hook must never stop Claude Code
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
