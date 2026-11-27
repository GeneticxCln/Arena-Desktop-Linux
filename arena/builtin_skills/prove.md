---
name: prove
description: Prove a change with the narrowest real check - baseline, smallest edit, the exact command, the first error, and the output. Use before claiming a fix works.
tools: edit_file, write_file, run_cmd, read_file
---

# Prove a change

A claim that a change works is worth what the command that showed it is worth.
This is the loop that produces that command and its output.

## The loop

1. **Choose the check before you edit.** Pick the narrowest real command that
   proves the change: one test file, a typecheck, a build, or the app with one
   request. Write the command down so you run the same one each time.
2. **Run it once before the edit** when you can. You need the baseline: already
   passing, already failing, or failing for another reason.
3. **Make the smallest edit that should satisfy the check.** One change at a
   time, so you know what fixed it.
4. **Run the exact command with `run_cmd`.** Read the whole error: the first
   error, its file and line, and the frame in your code. The last line is
   usually fallout.
5. **State the root cause in one sentence**, based on the output. If you cannot,
   read the failing code or add one log line, then rerun. Do not guess.
6. **Fix the cause, not the symptom.** Do not special-case the input the test
   happens to use.
7. **Rerun the same command.** That is one iteration.
8. **When the narrow check passes, run the wider one once**: the full suite,
   the build or the linter.
9. **Paste the command and the last lines of its output.** A claim without the
   output does not count.

## Stop when

- The check passes and you have pasted its output.
- Five iterations without passing: stop editing, report the last error, and
  load `unstick`.
- The same error comes back after two different fixes: load `unstick` now.
- No check exists: write the smallest one that fails without the change, then
  use this loop.

## Rules

- Never weaken, skip, delete or loosen a test, assertion, type or lint rule to
  make the check pass. If the test is wrong, say why and ask.
- Never mock away the thing under test.
- Run the real command every time. Do not reason about what it would print.
- A check you could not run (a command the person denied, Plan mode) is a limit
  to report, not a result to assume.
