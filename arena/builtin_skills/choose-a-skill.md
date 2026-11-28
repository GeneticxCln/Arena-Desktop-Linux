---
name: choose-a-skill
description: Pick the one skill that fits the task, say why, and load it instead of stacking several overlapping ones.
tools: use_skill
---

# Choose a skill

The menu is short enough to choose from directly. Use this only when the task
has more than one plausible skill.

| If the task is... | Load |
|---|---|
| more than one file or step | `plan-loop` |
| a bug with a cheap local check | `test-first`, then `prove` |
| a claim that a change works | `prove` |
| the same failure after two fixes | `unstick` |
| a note or comment that points at one place | `scoped-fix` |
| a review of a change before a person merges it | `review-loop` |
| a wide but shallow audit (every file, route, key) | `wide-audit` |
| a security review | `security-review` |
| an explanation of how code works | `explain-code` |
| how shipped software (an app, binary, APK, bundle) works | `reverse-engineer` |
| a short video, animation, explainer or title card | `motion-video` |
| a design or debugging decision | `engineering-principles` |
| prose, docs or comments that sound generated | `plain-prose` |

## Rules

- Recommend one skill. Name a second only when the task clearly has two phases,
  such as plan then prove.
- If no skill fits, answer directly.
- Loading a skill is guidance about order, not permission. It cannot add a tool
  or approve a change; every tool still runs under the permission mode.
