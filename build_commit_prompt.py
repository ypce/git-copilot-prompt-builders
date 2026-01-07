#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
build_commit_prompt.py

Create a Copilot-ready commit prompt from git changes and save it to prompt.md.

Default: uses STAGED changes (index). You can switch to UNSTAGED or provide a RANGE.

Examples:
  # 1) Staged (default)
  python build_commit_prompt.py

  # 2) Unstaged working tree
  python build_commit_prompt.py --unstaged

  # 3) Explicit range
  python build_commit_prompt.py --range HEAD~3..HEAD

  # 4) Longer truncation limit
  python build_commit_prompt.py --max-chars 50000
"""

import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from textwrap import shorten


# ---------- CLI args ----------
def get_flag(args, name, default=None, expects_value=False):
    if expects_value:
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return default
    else:
        return any(a == name for a in args)


def parse_mode(argv):
    rng = get_flag(argv, "--range", None, expects_value=True)
    unst = get_flag(argv, "--unstaged")
    if rng:
        return ("RANGE", rng)
    if unst:
        return ("UNSTAGED", None)
    return ("STAGED", None)


def get_limit(argv):
    v = get_flag(argv, "--max-chars", "30000", expects_value=True)
    try:
        return max(2000, int(v))
    except:
        return 30000


# ---------- subprocess ----------
def run(cmd, cwd=None, check=True):
    p = subprocess.run(
        shlex.split(cmd),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{p.stderr}")
    return p.stdout


def ensure_git_repo():
    try:
        run("git rev-parse --is-inside-work-tree")
    except Exception:
        print("ERROR: Not a git repository. Run inside a repo.", file=sys.stderr)
        sys.exit(2)


# ---------- classification helpers ----------
def file_type(path):
    ext = os.path.splitext(path.lower())[1]
    mapping = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".java": "Java",
        ".cs": "C#",
        ".go": "Go",
        ".rb": "Ruby",
        ".php": "PHP",
        ".rs": "Rust",
        ".kt": "Kotlin",
        ".swift": "Swift",
        ".css": "Styles",
        ".scss": "Styles",
        ".sass": "Styles",
        ".html": "HTML",
        ".htm": "HTML",
        ".yml": "YAML",
        ".yaml": "YAML",
        ".json": "JSON",
        ".md": "Docs",
        ".sh": "Shell",
        ".bash": "Shell",
        ".sql": "SQL",
    }
    return mapping.get(ext, ext[1:].upper() if ext else "Other")


KEYWORD_HINTS = [
    (re.compile(r"\bfix(e[sd])?\b|\bbug(s)?\b", re.I), "fix"),
    (re.compile(r"\brefactor|cleanup|restructure\b", re.I), "refactor"),
    (re.compile(r"\bfeat(ure)?\b|\badd(ed)?\b|\bimplement(ed)?\b", re.I), "feat"),
    (re.compile(r"\bperf(ormance)?\b|\boptimi[sz]e(d)?\b", re.I), "perf"),
    (re.compile(r"\bdoc(s|umentation)?\b|\breadme\b", re.I), "docs"),
    (re.compile(r"\btest(s|ing)?\b|\bunit[- ]?test\b|\bci\b", re.I), "test"),
    (re.compile(r"\bbuild|pipeline|ci/cd|github actions|workflow\b", re.I), "build"),
    (re.compile(r"\bsec(urity)?\b|\bsecret(s)?\b|\bvuln", re.I), "security"),
    (
        re.compile(r"\bchore\b|\bdeps?\b|\bdependency\b|\bupgrade\b|\bupdate\b", re.I),
        "chore",
    ),
]


def conventional_prefix(hints):
    # pick one if clear
    priority = [
        "fix",
        "security",
        "feat",
        "perf",
        "refactor",
        "build",
        "test",
        "docs",
        "chore",
    ]
    for p in priority:
        if p in hints:
            return p
    return None


# ---------- diff collection ----------
def collect_diff(mode, rng):
    if mode == "STAGED":
        numstat_cmd = "git diff --cached --numstat"
        patch_cmd = "git diff --cached"
        source_lab = "index (staged)"
    elif mode == "UNSTAGED":
        numstat_cmd = "git diff --numstat"
        patch_cmd = "git diff"
        source_lab = "working tree (unstaged)"
    else:
        numstat_cmd = f"git diff --numstat {rng}"
        patch_cmd = f"git diff {rng}"
        source_lab = f"range {rng}"
    return numstat_cmd, patch_cmd, source_lab


def summarize(numstat_out, patch_text):
    files = []
    langs = Counter()
    add_total = del_total = 0
    for line in numstat_out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        try:
            a = int(a)
        except:
            a = 0
        try:
            d = int(d)
        except:
            d = 0
        add_total += a
        del_total += d
        kind = file_type(path)
        langs[kind] += 1
        files.append({"path": path, "added": a, "deleted": d, "kind": kind})

    hints = Counter()
    interesting = defaultdict(list)

    def consider(text, path=None):
        for rx, tag in KEYWORD_HINTS:
            if rx.search(text):
                hints[tag] += 1
                if path and len(interesting[path]) < 3:
                    interesting[path].append(text.strip())

    for f in files:
        consider(f["path"], f["path"])

    for line in patch_text.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            consider(line)

    return {
        "files": files,
        "languages": langs,
        "added": add_total,
        "deleted": del_total,
        "hints": hints,
        "interesting": interesting,
    }


# ---------- secret scrubbing & truncation ----------
SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|secret|password|token|sas|connection[_-]?string)\s*[:=]\s*[\"']?([A-Za-z0-9_\-\/\.\+=]{6,})"
    ),
    re.compile(
        r"(?i)-----BEGIN ([A-Z ]+?) PRIVATE KEY-----.*?-----END \1 PRIVATE KEY-----",
        re.S,
    ),
    re.compile(r"(?i)\"clientSecret\"\s*:\s*\"[^\"]{6,}\""),
]


def scrub(text: str) -> str:
    t = text
    t = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=***", t)
    t = SECRET_PATTERNS[1].sub(
        "-----BEGIN PRIVATE KEY-----\n***\n-----END PRIVATE KEY-----", t
    )
    t = SECRET_PATTERNS[2].sub('"clientSecret":"***"', t)
    return t


# ---------- prompt building ----------
def craft_title(data, max_len=72):
    kinds = [k for k, _ in data["languages"].most_common(3)]
    kind_str = ", ".join(kinds).lower() if kinds else "files"
    prefix = conventional_prefix(set(data["hints"].keys()))
    core = f"{prefix}: {kind_str}" if prefix else f"update {kind_str}"
    extra = []
    if data["added"] and data["deleted"]:
        extra.append(f"+{data['added']}/-{data['deleted']}")
    elif data["added"]:
        extra.append(f"+{data['added']}")
    elif data["deleted"]:
        extra.append(f"-{data['deleted']}")
    if data["files"]:
        extra.append(f"{len(data['files'])} files")
    t = f"{core} ({', '.join(extra)})" if extra else core
    return shorten(re.sub(r"\s+", " ", t).strip(), width=max_len, placeholder="…")


def build_prompt(repo_name, source_label, data, safe_diff):
    # A lean, paste-ready instruction set for M365 Copilot chat
    title_guess = craft_title(data)
    langs_line = (
        ", ".join(f"{k.lower()}×{v}" for k, v in data["languages"].most_common())
        or "n/a"
    )
    change_types = (
        ", ".join(f"{k}({v})" for k, v in data["hints"].most_common()) or "n/a"
    )

    prompt = f"""You are an expert release engineer.

Draft a Git commit message for the repository "{repo_name}". Follow these rules:

- **Summary**: one line ≤ 72 chars. Prefer a conventional type if clear (feat, fix, refactor, docs, test, build, chore, perf, security).
- **Description**: 4–8 bullets. Each bullet should be a concrete change or impact, optionally citing key files/modules.
- Keep language concise and action‑oriented.
- Avoid secrets. Do not include tokens, passwords, or private keys in the message.

Context
- Source of changes: {source_label}
- Detected languages/files: {langs_line}
- Detected change types: {change_types}
- Heuristic title suggestion: "{title_guess}"

Now produce ONLY this format:

Summary: <your single-line title>
Description:
- <bullet 1>
- <bullet 2>
- <bullet 3>
- <bullet 4>

Diff (secret‑scrubbed, may be truncated)

{safe_diff}
"""
    return prompt


# ---------- main ----------
def main():
    ensure_git_repo()

    mode, rng = parse_mode(sys.argv[1:])
    limit = get_limit(sys.argv[1:])

    numstat_cmd, patch_cmd, source_label = collect_diff(mode, rng)

    try:
        numstat_out = run(numstat_cmd)
        patch_text = run(patch_cmd)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Summaries + safe diff
    data = summarize(numstat_out, patch_text)
    safe = scrub(patch_text)
    if len(safe) > limit:
        safe = safe[:limit] + "\n… (truncated)"

    # Repo name
    try:
        repo_name = run("git rev-parse --show-toplevel").strip().split(os.sep)[-1]
    except Exception:
        repo_name = "unknown-repo"

    prompt_text = build_prompt(repo_name, source_label, data, safe)

    out_path = os.path.join(os.getcwd(), "prompt.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)

    print(f"Wrote Copilot-ready prompt → {out_path}")
    print(
        f"Source: {source_label} | Files: {len(data['files'])} | +{data['added']}/-{data['deleted']}"
    )


if __name__ == "__main__":
    import re

    main()
