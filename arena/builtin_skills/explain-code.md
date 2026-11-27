---
name: explain-code
description: Explain how a piece of code works and why it has its shape, in plain language, citing the files and lines and labelling inference.
tools: ls, read_file, read_files, search, run_cmd
---

# Explain code

Give one plain account of what a thing is, how it runs, and why it has its
current shape, so the reader can change it with confidence.

## The loop

1. **Work out what the reader needs.** Are they about to change it, review it,
   debug it, or are they new? Pick the three or four things they should walk
   away knowing. Skip what they already know.
2. **Trace the real path.** Use `search` and `read_file` (or `read_files` for
   several at once) from entry point to effect. Note who owns each piece of
   state and where the boundaries are. Cite `path:line` for every claim about
   behaviour.
3. **Find why it is that way.** In a git repository, `run_cmd` with
   `git log -L` or `git log --follow -- <file>` for the commits near the code.
   Check `MEMORY.md` for what earlier chats established. Separate what the
   record says from what you infer, and label the inference. If no record
   exists, say the reason is unknown.
4. **Write the explanation.** Start with a plain definition, then tie it to this
   codebase. Walk through what happens when someone uses it. Then the reasons,
   then the edge cases. Use a small table when it helps.
5. **Offer to go deeper on one named part.**

## Rules

- Do not edit files. This skill changes nothing.
- Explain ideas, not a list of functions.
- Never present a guess about intent as fact.
- If three rounds of searching find no reason, say it is unknown rather than
  inventing one.
