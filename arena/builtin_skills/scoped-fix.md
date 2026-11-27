---
name: scoped-fix
description: Fix exactly what was pointed at - write a fix card, change only that scope, and prove everything outside it is unchanged before saying it is done.
tools: read_file, search, edit_file, write_file, run_cmd, set_todos, ask_user
---

# Scoped fix

A note, a review comment or a bug report points at a place. The usual failure is
not a wrong fix; it is a right fix with three other things moved. This skill
makes the boundary part of the work and part of the proof.

## The fix card

Write it with `set_todos` before any edit:

- **Scope**: the place the note points at (`path:line`, a component, a route).
- **Must not change**: what has to stay exactly as it is: neighbouring
  behaviour, a public signature, copy, formatting, a snapshot, a lockfile.
- **The failure now**: the observable, reproduced or quoted. If the note is a
  feeling ("it is slow", "it looks off"), turn it into the mechanism and the
  measure behind it first.
- **The target**: an observable that says it is fixed: a command and its
  output, a value, a state.
- **Shortcuts that would fake it**: loosening an assertion, special-casing the
  input, a longer timeout, a skipped test, an updated snapshot, a swallowed
  error, a suppressed lint. Write them down so you can refuse them.

## Ambiguity

If a note has two readings, take the more sensible one, do it, and say in the
report which reading you took. Ask once with `ask_user` only when the readings
lead to different work and the wrong one is costly.

## Make the change

Change one cause at a time and keep it small. With several notes, take the ones
that block understanding first, structure before surface, one per step.

## Prove the boundary

1. **The target**, with the exact `run_cmd` command, before and after.
2. **Nothing outside the scope moved.** In a git repository, run
   `git diff --stat` and `git diff`, and list every changed path. Each one is
   inside the scope or named with the reason it had to change (an import, a type).
3. **Compare sources, not products.** A rebuilt bundle or regenerated file
   differs for reasons that are not your edit. Diff the source.
4. **The neighbours still pass.** Run the checks that cover the code next to the
   scope, and re-check the original failure.

## Report

The card; the target before and after; the paths changed; the reading taken;
what was not checked.

## Stop when

- The target is observed and every change outside the scope is named and justified.
- Two attempts show the cause is outside the scope. Report it; widening the scope
  is the person's call.
- The only fix needs a change the card lists under must-not-change: stop and ask.
