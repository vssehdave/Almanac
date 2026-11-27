"""PreToolUse: the lead does not write code; builders do. Only a one-line edit is allowed on the main thread."""
import os
import sys

import guardlib

REASON = ("Blocked: the lead does not write code. Send this function to almanac:builder (one function per builder); "
          "only a one-line edit is done on the main thread.")

CODE_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".c", ".h",
                 ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".ps1", ".ipynb"}


def _one_line(text):
    return "\n" not in str(text).strip("\n")


def _edit_ok(edit):
    return _one_line(edit.get("old_string", "")) and _one_line(edit.get("new_string", ""))


def check(tool_name, tool_input, payload):
    if payload.get("agent_type"):  # sub-agents are the builders
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if os.path.splitext(path)[1].lower() not in CODE_SUFFIXES:
        return None
    if tool_name in ("Write", "NotebookEdit"):
        return REASON
    if tool_name == "Edit":
        return None if _edit_ok(tool_input) else REASON
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        ok = isinstance(edits, list) and len(edits) == 1 and isinstance(edits[0], dict) and _edit_ok(edits[0])
        return None if ok else REASON
    return None


if __name__ == "__main__":
    sys.exit(guardlib.run(check, payload=True))
