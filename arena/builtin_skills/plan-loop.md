---
name: plan-loop
description: Read the files first, write a numbered plan with one check per step, critique it once, then execute one verified step at a time.
tools: ls, read_file, read_files, search, propose_plan, set_todos, edit_file, write_file, run_cmd
---

# Plan loop

## The loop

1. **Restate the goal and read the files.** List the files and commands the work
   will touch, then read them (`read_files` for several at once). Do not plan
   from memory.
2. **Write a numbered plan.** Each step has one action and one acceptance check
   that a command or a direct observation can confirm. Keep each step small
   enough for one diff. Present it with `propose_plan` so the person can approve
   it in one click.
3. **Critique it once** as a fresh pass: which setup, migration, config, test or
   cleanup step is missing; which assumption was not checked; is there a shorter
   path? Do not run a third critique.
4. **Check each risky assumption now** with `read_file` or a read-only `run_cmd`;
   do not carry it as a guess.
5. **Execute step one only.** Make the change, then run its check and read the
   real output. If the step shows the plan is wrong, stop and revise the
   remaining steps instead of improvising around it.
6. **Record the evidence** with `set_todos` (`[x]` done, `[~]` in progress,
   `[ ]` pending), then take the next step.

## Rules

- No step without a check. "Looks right" is not a check.
- One step per diff is what makes each check mean something.
- In Plan mode only reads run: stop after `propose_plan` and wait.

## Stop when

- Every step is done, its check passed, and the whole goal holds end to end.
- A step fails its check twice in a row: load `unstick` instead of a third variation.
