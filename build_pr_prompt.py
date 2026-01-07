#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
build_pr_prompt.py

Create a Copilot-ready Pull Request (PR) prompt from git changes and save it to pr_prompt.md.

Defaults:
  - Uses STAGED changes (index).
  - If there are no staged/unstaged changes AND you did not pass --range/--against,
    it auto-falls back to comparing the current HEAD against the repo default base
    (typically origin/main) using a three-dot diff: <base>...HEAD.

Examples:
  # 1) Auto (staged, or fallback to origin/main...HEAD if nothing staged/unstaged)
  python build_pr_prompt.py

  # 2) Unstaged working tree
  python build_pr_prompt.py --unstaged

  # 3) Explicit range (two- or three-dot)
  python build_pr_prompt.py --range origin/main...HEAD

  # 4) Explicit base branch (three-dot diff)
  python build_pr_prompt.py --against origin/release/2025.11

  # 5) Longer truncation limit
  python build_pr_prompt.py --max-chars 50000
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


def get_against(argv):
    return get_flag(argv, "--against", None, expects_value=True)


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


# ---------- base branch detection ----------
def detect_default_base():
    """
    Try to detect the default base branch for comparisons, preferring origin/HEAD.
    Fallback guesses: origin/main, main, origin/master, master (first that exists).
    Returns a ref string usable in git diff commands.
    """
    try:
        # e.g. 'refs/remotes/origin/main' -> 'origin/main'
        ref = run(
            "git symbolic-ref --quiet refs/remotes/origin/HEAD", check=True
        ).strip()
        if ref.startswith("refs/remotes/"):
            return ref[len("refs/remotes/") :]
    except Exception:
        pass

    for guess in ("origin/main", "main", "origin/master", "master"):
        try:
            run(f"git rev-parse --verify {guess}", check=True)
            return guess
        except Exception:
            continue
    return "main"


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
        ".dockerfile": "Docker",
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
def collect_diff(mode, rng, base=None, head="HEAD"):
    """
    Returns (numstat_cmd, patch_cmd, source_label)
    If base is provided, uses a three-dot diff: base...head
    """
    find_renames = "--find-renames"
    if base:
        numstat_cmd = f"git diff {find_renames} --numstat {base}...{head}"
        patch_cmd = f"git diff {find_renames} {base}...{head}"
        source_lab = f"branch delta {base}...{head}"
        return numstat_cmd, patch_cmd, source_lab

    if mode == "STAGED":
        numstat_cmd = f"git diff {find_renames} --cached --numstat"
        patch_cmd = f"git diff {find_renames} --cached"
        source_lab = "index (staged)"
    elif mode == "UNSTAGED":
        numstat_cmd = f"git diff {find_renames} --numstat"
        patch_cmd = f"git diff {find_renames}"
        source_lab = "working tree (unstaged)"
    else:
        numstat_cmd = f"git diff {find_renames} --numstat {rng}"
        patch_cmd = f"git diff {find_renames} {rng}"
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


# ---------- PR prompt building ----------
PR_HINT = (
    "Follow enterprise GitHub usage policies (branch naming, commit formatting, verified email & signed commits). "
    "Avoid secrets; do not include tokens, passwords, or private keys. "
    "Reference linked issue/Jira; describe risks, security & compliance notes."
)


def craft_title_guess(data, max_len=72):
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


def build_pr_prompt(repo_name, source_label, data, safe_diff):
    title_guess = craft_title_guess(data)
    langs_line = (
        ", ".join(f"{k.lower()}×{v}" for k, v in data["languages"].most_common())
        or "n/a"
    )
    change_types = (
        ", ".join(f"{k}({v})" for k, v in data["hints"].most_common()) or "n/a"
    )

    prompt = f"""You are an experienced enterprise PR author.

Draft a Pull Request (PR) **title and description** for repository "{repo_name}".
Honor these enterprise-aligned rules:
- **Title**: one line ≤ 72 chars. Prefer conventional type if clear (feat, fix, refactor, docs, test, build, chore, perf, security).
- **Body**: use the sections below; keep language concise and action-oriented.
- **Security/Compliance**: avoid secrets; include security impact and any policy references.
- **Standards**: follow branch naming & signed commits/verified email expectations; link the tracking work item (Issue/Jira).

Context
- Source of changes: {source_label}
- Detected languages/files: {langs_line}
- Detected change types: {change_types}
- Heuristic title suggestion: "{title_guess}"
- Note: {PR_HINT}

Now produce ONLY this format:

Title: <concise title here>

## Summary
- <what and why in 1–3 bullets>

## Changes
- <bullet of concrete code changes or modules touched>
- <additional bullets...>

## Testing
- <test approach, coverage, environments>
- <how reviewers can validate locally>

## Risk & Rollback
- Risk level: <Low|Medium|High> and reasoning
- Rollback plan: <how to revert/feature-flag/backout>

## Security & Compliance
- Secrets/PII: <none|details of handling>
- Security impact: <dependency bumps, auth, scopes, permissions>
- Policy notes: <signed commits/verified email/branch naming/required checks>

## Links
- Issue/Jira: <#123 or URL>
- Related docs/runbooks: <URLs>

## Checklist
- [ ] Branch name follows team convention
- [ ] Commits are **signed** and email is **verified**
- [ ] CI passes; required checks green
- [ ] No secrets in code or PR text
- [ ] Reviewers/labels/milestone set

--- DIFF (secret‑scrubbed, may be truncated) ---

{safe_diff}
"""
    return prompt


# ---------- main ----------
def main():
    ensure_git_repo()

    mode, rng = parse_mode(sys.argv[1:])
    limit = get_limit(sys.argv[1:])
    against = get_against(sys.argv[1:])
    base_fallback = detect_default_base()

    # initial selection
    numstat_cmd, patch_cmd, source_label = collect_diff(mode, rng, base=against)

    try:
        numstat_out = run(numstat_cmd)
        patch_text = run(patch_cmd)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Auto-fallback: if empty and user did not force range/base, compare base_fallback...HEAD
    if not numstat_out.strip() and not patch_text.strip():
        if mode in ("STAGED", "UNSTAGED") and not rng and not against:
            numstat_cmd, patch_cmd, source_label = collect_diff(
                mode="RANGE", rng=None, base=base_fallback, head="HEAD"
            )
            try:
                numstat_out = run(numstat_cmd)
                patch_text = run(patch_cmd)
                if numstat_out.strip() or patch_text.strip():
                    print(f"(No working tree changes; showing {source_label})")
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

    prompt_text = build_pr_prompt(repo_name, source_label, data, safe)

    out_path = os.path.join(os.getcwd(), "pr_prompt.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)

    print(f"Wrote Copilot-ready PR prompt → {out_path}")
    print(
        f"Source: {source_label} | Files: {len(data['files'])} | +{data['added']}/-{data['deleted']}"
    )


if __name__ == "__main__":
    main()
