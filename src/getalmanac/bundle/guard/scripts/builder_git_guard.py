"""PreToolUse: block git commands run by the almanac:builder agent. The lead commits, not the builders."""
import re
import sys

import guardlib

REASON = ("Blocked: builders do not use git. Leave the changes in the worktree and report; the lead commits.")

_git = re.compile(r"(^|[;&|(\n]|\$\()\s*(?:rtk\s+)?(?:env\s+)?(?:\w+=\S*\s+)*(?:\"[^\"\n]*[\\/]|'[^'\n]*[\\/]|[^\s\"';&|]*[\\/])?[\"']?git(?:\.exe)?[\"']?(?=[\s;&|)]|$)")


def check(tool_name, tool_input, payload):
    if payload.get("agent_type") != "almanac:builder":
        return None
    command = tool_input.get("command", "")
    if _git.search(command):
        return REASON
    return None


if __name__ == "__main__":
    sys.exit(guardlib.run(check, payload=True))
