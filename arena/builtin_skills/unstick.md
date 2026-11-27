---
name: unstick
description: Break a repeating failure - name the assumption two fixes shared, probe it once, then fix the cause or change mechanism. Load when the same error survives two attempts.
tools: read_file, search, run_cmd, edit_file
---

# Unstick a repeating failure

Two fixes that failed the same way are one wrong belief, not two bad edits. The
work is to find the belief and test it, because a third variation of the same
edit is a third sample of the same mistake.

## When this is for

The same error (the same message, failed assertion or wrong behaviour) comes back
after two attempts. Two different errors are ordinary progress, not this.

## The loop

1. **Stop editing.** Do not try a third variation of the fix.
2. **Write the attempts down**, one line each: what you changed, and the exact
   error it produced. Keep the first error, not the last line.
3. **Name the shared assumption** in one sentence. What did both attempts
   believe and never check? "This file is the one that runs", "the config is
   read from here", "the function receives a string", "the cache is cleared".
4. **Probe it once**, with the cheapest call that answers yes or no:
   `read_file` the real call site rather than the file you assumed; `search` for
   where a value is actually read, imported or defined; `run_cmd` a narrow check.
   Run it and read the output; do not reason about what it would print.
5. **Read the probe literally.** If the assumption is false, that is the bug:
   fix what the output shows.
6. **If the assumption holds**, the fix is in the wrong place. List two other
   mechanisms (a different layer, call path or tool) and say in one line why
   each might work. Pick one and change the mechanism, not the edit.
7. **Make one attempt at the new mechanism**, then run the check for real.

## What a probe cannot be

- A command the person denied, re-run another way to get around the denial.
- A test you weakened. If the probe says the test is wrong, say so and let the
  person decide.

## Stop when

- The check passes and you have its output.
- Two rounds of this have failed. Report the attempts, the assumption you
  tested, the probe's exact output, and what you would try next.

## What to tell the person

The false assumption, in one sentence, and the output that showed it.
