---
name: wide-audit
description: Split a wide but shallow audit into non-overlapping slices, check each with search and read tools, and report evidence or a recorded gap for every slice.
tools: ls, search, read_file, read_files, set_todos, run_cmd
---

# Wide audit

For coverage, not depth: every file, endpoint, config key or call site in a
named set. Wide enough that one search is not enough, deep enough that a list of
filenames is not an answer.

## The loop

1. **State the done condition and the report.** What set is being covered, and
   what does a slice's result look like? A result is PASS, ISSUES or BLOCKED,
   with the file or command that showed it.
2. **Partition into non-overlapping slices** by directory, module, route, config
   or symbol. Write the slices down with `set_todos` so a missing one is visible.
3. **Check one slice at a time.** `search` for names and call sites, `read_file`
   or `read_files` for the code a hit points at. Batch independent reads in one
   reply. Do not count a hit as a finding until you have read it.
4. **Record each slice's result with evidence.** ISSUES lists every provable
   issue, not only the first. BLOCKED says what could not be read and why. A
   slice with no evidence is not PASS.
5. **Merge by root cause** into one report: the verdict, each slice with its
   evidence, the gaps, duplicates collapsed. A missing slice never counts as passing.

## Rules

- A search result covers the files it searched. Say what it skipped rather than
  turning a bounded search into "none exist".
- A hit is a candidate. Read the code before reporting it.
- Do not fix anything here. Hand real findings back as a plan.
- For a very wide audit, sub-agents (when this chat offers them) can take slices;
  each slice still needs its own evidence.
