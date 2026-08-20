#!/usr/bin/env python3
"""PreToolUse/Bash guard: refuse a `git commit` that breaks a CLAUDE.md rule.

Two rules, both of which have been violated in this repo before:

  (a) Never commit on `main`. Branch, then merge when asked.
  (b) No mention of Claude or Anthropic in a commit message, including a
      `Co-Authored-By` trailer.

Exit 2 blocks the tool call and shows stderr to the model; exit 0 allows it.

`git merge` is deliberately NOT matched: merging into main is the sanctioned
workflow, and only a direct `git commit` on main is the mistake.

Why this parses the command instead of grepping it: the first version grepped the
whole command line for "claude", which refused a perfectly clean commit whenever
a path happened to contain the word -- `/tmp/claude-*/...` or `~/.claude/msg.txt`.
The banned word has to be looked for in the MESSAGE, so the message is what gets
extracted.

Known limitation: the branch is read from the project repo, so a command that
first `cd`s into a different repository is judged against the wrong branch.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

BANNED = re.compile(r"claude|anthropic", re.I)
MSG_OPTS = ("-m", "--message")
FILE_OPTS = ("-F", "--file")
# Global flags that may sit between `git` and the subcommand, and take a value.
GLOBAL_VALUE_FLAGS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")


def refuse(reason: str) -> None:
    print(f"BLOCKED by .claude/hooks/guard-commit.py — {reason}", file=sys.stderr)
    sys.exit(2)


def commit_args(tokens: list[str]) -> list[str] | None:
    """Return the args following `git ... commit`, or None if this isn't one.

    `commit` must be the git SUBCOMMAND, not merely a word on the line, or
    `git log --grep=commit` would be refused.
    """
    for i, tok in enumerate(tokens):
        if tok != "git" and not tok.endswith("/git"):
            continue
        j = i + 1
        while j < len(tokens):
            t = tokens[j]
            if t in GLOBAL_VALUE_FLAGS:      # flag and its separate value
                j += 2
            elif t.startswith("-"):          # --flag=value, or a lone switch
                j += 1
            else:
                break
        if j < len(tokens) and tokens[j] == "commit":
            return tokens[j + 1:]
    return None


def message_text(args: list[str]) -> str:
    """Everything that will become the commit message, from -m and -F."""
    parts: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        for opt in MSG_OPTS + FILE_OPTS:
            value = None
            if a == opt and i + 1 < len(args):
                value, i = args[i + 1], i + 1
            elif a.startswith(opt + "="):
                value = a[len(opt) + 1:]
            elif opt == "-m" and len(a) > 2 and a.startswith("-") and not a.startswith("--") \
                    and "m" in a[1:] and a[-1] == "m" and i + 1 < len(args):
                value, i = args[i + 1], i + 1      # combined form, e.g. -am
            if value is None:
                continue
            if opt in FILE_OPTS:
                try:
                    with open(os.path.expanduser(value), "r", errors="replace") as fh:
                        parts.append(fh.read())
                except OSError:
                    pass                            # unreadable: nothing to judge
            else:
                parts.append(value)
            break
        i += 1
    return "\n".join(parts)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)                                 # not our business
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        sys.exit(0)

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()                        # unbalanced quotes: best effort

    args = commit_args(tokens)
    if args is None:
        sys.exit(0)

    # ---- (a) never commit on main -----------------------------------------
    repo = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        branch = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""

    if branch in ("main", "master"):
        refuse(
            f'never commit on `{branch}` (CLAUDE.md, "How to work here").\n'
            "Branch first, then merge when the owner asks:\n"
            "    git switch -c <topic>\n"
            "    git commit ...\n"
            "Merges here have been fast-forward every time; keep it that way."
        )

    # ---- (b) no Claude/Anthropic in the message ----------------------------
    if BANNED.search(message_text(args)):
        refuse(
            "the commit message mentions Claude or Anthropic.\n"
            "CLAUDE.md: no mention of Claude or Anthropic in any commit message, PR\n"
            "body, tag or release note, and no `Co-Authored-By: Claude` trailer.\n"
            "Rewrite it as prose naming the defect and the reasoning, and drop the\n"
            "trailer. (The 25 already-pushed commits carrying one are settled.)"
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
