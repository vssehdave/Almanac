---
name: handover
description: Hand work back to the lead in a fixed format. Use when stopping at the context cap, after a cold compaction, or whenever handing work back to the lead.
---

# Handover

Make your final message exactly these seven sections, in this order, short:

1. **Task**: one line.
2. **Done**: what is finished, with commit ids.
3. **Left**: an ordered list of what remains, first step first.
4. **Files touched**: paths.
5. **Verified**: what you checked, and how (the command and what it printed).
6. **Not verified**: what you did not check.
7. **Blocked / needs the owner**: anything you cannot do alone, or "none".

If the memory_handover tool is available, also save these notes with it.

When work is left, end with this line, exactly and last:

HANDOVER: continue with a fresh <agent_type> agent

Replace `<agent_type>` with your own agent type. When nothing is left, leave the line out.
