"""A status for every stored exchange: what compaction needs to know to keep the right things.

    open        still needs something: the newest exchange, or one that ended on a question
    done        finished, nothing standing
    decision    the person stated a rule or a choice that should keep holding
    superseded  the person's next message said the attempt went wrong
    noise       an acknowledgement, or a command echo

The rules are plain code and run whenever an exchange is stored, again for the
conversation's last few exchanges (a later message can change an earlier one's
status). A small model may refine a status when it writes the summary (see
summarize.py): its value replaces the rule's, except that a rule-made `decision`
or `superseded` is never downgraded.
"""
import re

from . import request as rq

STATUSES = ("open", "done", "decision", "superseded", "noise")
OPEN_KEEP = 15  # an exchange this far behind the newest is no longer listed as open: nobody closes them by hand
# The one place the wording of a standing rule lives. Matched on the text the person wrote by hand, lower-cased.
DECISION = re.compile(
    r"\b(from now on|going forward|always|never|remember that|no longer|instead of|i only need|keep it at"
    r"|don[’']?t\b[^.!?\n]{0,60}\bagain|we(?:[’']re| are) not)\b")


def _tools(group) -> bool:
    return any(b.get("type") == "tool_use" for m in group[1:] if m.get("role") == "assistant" for b in rq.blocks(m))


def _ends_on_question(group) -> bool:
    for msg in reversed(group[1:]):
        if msg.get("role") == "assistant":
            for block in reversed(rq.blocks(msg)):
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    return block["text"].rstrip(" \t\r\n*_`\"')").endswith("?")
    return False


def rule(group, following=None, newest=False) -> str:
    """The status the rules give one exchange. `following` is the next human message's text, if there is one."""
    from . import routing  # routing imports memory, which imports this module

    typed = routing.person_text(group[0])
    mine = routing.handwritten(typed)
    if DECISION.search(mine) and not routing.QUESTION.match(mine):  # "why does it always fail?" sets no rule
        return "decision"
    if following and routing.failed(following):
        return "superseded"
    if not typed or (routing.CONTINUE.match(mine) and not _tools(group)):
        return "noise"
    return "open" if newest or _ends_on_question(group) else "done"


def combine(by_rule: str, by_model: str = "") -> str:
    """The status kept: the model's refinement, unless the rules made a decision or a superseded mark."""
    if by_rule in ("decision", "superseded") or by_model not in STATUSES:
        return by_rule
    return by_model
