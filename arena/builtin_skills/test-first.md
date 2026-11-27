---
name: test-first
description: Make a bug executable first - write the smallest test that fails for the right reason, then make the smallest fix and show both runs.
tools: read_file, search, write_file, edit_file, run_cmd
---

# Test first

Make the bug executable before touching production code, so the fix is proved
rather than claimed.

## The loop

1. **Pin down the bug.** Expected behaviour, actual behaviour, the code path and
   the smallest way to reproduce it. `read_file` the path; do not infer it from
   the report.
2. **Find the nearest existing test style.** Read the test file beside the code
   and use its helpers. Do not build a new harness for one test.
3. **Write the smallest failing test.** It checks intended behaviour, not the
   current implementation.
4. **Run it with `run_cmd` and show the output.** It must fail, and fail because
   of the bug, not a typo or a missing import. If it fails for the wrong reason,
   fix the test first.
5. **Make the smallest production change** that gives the intended behaviour
   without breaking a nearby contract, then run the test and its neighbours.
6. **Show both runs.** The test failed before the fix and passes after it. A
   passing test with no failing baseline proves less.
7. **If a clean test is impractical** (heavy setup, production-only state, a
   timing-only failure), use the closest runnable check: a script, a
   reproduction command or a log check. Say why the test was not written.

## Stop when

- The new test failed for the right reason before the fix and passes after it,
  and both runs are shown.
- Three fix attempts have failed: load `unstick` instead of a fourth variation.
- No test can express the bug: say so and stop, rather than write one that
  mostly tests mocks.

## Rules

- Never edit a test to match a wrong implementation.
- Do not weaken an existing assertion without a stated reason.
- No test beats a bad test: one that mostly tests mocks, depends on timing, or
  would be deleted right after.
