---
name: review-loop
description: Review the scoped diff, judge each finding yourself, fix the smallest real ones, and re-review until nothing real is open or four rounds are done.
tools: run_cmd, read_file, search, edit_file
---

# Review loop

## The loop

1. **Scope the change.** In a git repository, `run_cmd` `git diff` (or
   `git diff main...HEAD`) to read the diff, and name the goal and what is out of
   scope. Run the covering checks first; do not review code that fails them.
2. **Review once, as a critic.** Read the diff and the code around it. Look for
   wrong behaviour, missed edge cases, broken contracts, missing tests and
   leftover debug code. If this chat offers sub-agents, a separate model can do
   this pass; say which one did.
3. **Judge each finding.** Real: make the smallest fix, then run the check.
   Not real: answer with evidence, a `path:line` or a command and its output. A
   nit or out-of-scope request gets one line and no change.
4. **Re-run the checks and review again**, only the open findings and the changed code.
5. **Report** the rounds, the fixed findings, the declined findings with reasons,
   and the verdict. If a finding is still disputed, give both sides in two or
   three lines and let the person decide.

## Rules

- A review that could not run is not approval.
- It does not push or merge. Those are the person's actions.

## Stop when

- No real finding is open and the checks pass.
- A round returns only nits.
- Four rounds are done, or one item stays disputed after two exchanges: report it.
