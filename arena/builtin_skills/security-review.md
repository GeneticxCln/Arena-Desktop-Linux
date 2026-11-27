---
name: security-review
description: Review this folder for common security weaknesses - search for risky patterns, read each hit, and report what is real. Use for a security review or an audit request.
tools: search, read_file, read_files, ls, run_cmd
---

# Security review

A pattern search finds candidates. It matches text and cannot see where a value
came from or where it goes, so most hits are noise or need context, and a few
are the bug. The work is the part a search cannot do: read the code around each
hit and decide. A review that forwards search hits unread is not a review.

## The order

1. **Map the program.** `ls` and `read_file` the entry points, the request
   handlers, the config loading and the dependency manifest. Note which inputs an
   outside party controls: request data, files, environment, command-line arguments.
2. **Search for sinks** that fit the languages here, for example:
   - injection: `subprocess`, `os.system`, `shell=True`, `eval(`, `exec(`, string-built SQL;
   - web: missing auth checks on routes, `innerHTML`, open redirects, CORS `*`;
   - files: paths joined from input without containment, `../`, symlinks followed;
   - secrets: `password`, `secret`, `token`, `api_key`, private-key headers;
   - crypto: `md5`, `sha1` for passwords, `random` for tokens, disabled TLS checks.
   If a dependency-audit command exists (`pip-audit`, `npm audit`), `run_cmd` it.
3. **Read before you decide.** For every hit, `read_file` the lines around it and
   follow the value one step back: where does it come from, and can an outside
   party influence it? `search` for the callers.
4. **Classify each one**, and say why in a sentence:
   - **Confirmed**: outside input reaches the sink and nothing in between stops it.
   - **Needs context**: it depends on something you could not see. Say what would settle it.
   - **False positive**: the value is a constant, is checked or escaped first, or the code never runs. Name which.
5. **Report**, highest severity first: `path:line`, the weakness, the evidence you
   read, and a concrete fix for the confirmed ones. Then the needs-context list,
   then a count of false positives. Say plainly what was not searched.

## What not to do

- Do not call something exploitable because a pattern matched.
- Do not claim a clean bill of health: "no pattern matched" means those searches
  found nothing in the files searched.
- Do not fix findings as part of the review. Offer a plan once the report is done.
- Secrets: say that a secret is present and where, and quote at most its first
  few characters.
