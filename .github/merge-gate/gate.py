#!/usr/bin/env python3
"""merge-gate: merge-safety decisions from Garnet execution behavior, at the PR gate.

Runs in GitHub Actions. Triggered when garnet-runtime-review[bot] posts a
Runtime Review comment on a PR (or manually via workflow_dispatch).

Reads the execution profile from the Runtime Review comment, infers PR type
from changed files, evaluates v1 merge-safety assertions, posts MERGE / HOLD /
AUDIT as a PR comment. Idempotent: skips if a gate comment already exists.

v1 policy scope: dependency PRs (manifest/lockfile-only changes).
"""
import json
import os
import re
import subprocess
import sys
import time

MARKER = "<!-- merge-gate-poc -->"
BOT = "garnet-runtime-review[bot]"

DEST_RULES = [
    (r"^registry\.npmjs\.org$", "package registry", "ok"),
    (r"^npmjs\.org$", "package registry", "ok"),
    (r".*\.npmjs\.org$", "package registry", "ok"),
    (r"^registry\.yarnpkg\.com$", "package registry", "ok"),
    (r"^nodejs\.org$", "toolchain download", "ok"),
    (r"^github\.com$", "source host", "ok"),
    (r"^codeload\.github\.com$", "source host", "ok"),
    (r"^release-assets\.githubusercontent\.com$", "release assets", "ok"),
    (r"^objects\.githubusercontent\.com$", "release assets", "ok"),
    (r"^api\.github\.com$", "CI api", "ok"),
    (r".*\.ubuntu\.com$", "os updates", "infra"),
    (r".*\.microsoft\.com$", "runner infra", "infra"),
    (r".*\.windows\.net$", "runner infra", "infra"),
    (r"^storage\.googleapis\.com$", "runner infra", "infra"),
    (r"^169\.254\.169\.254$", "cloud metadata", "infra"),
    (r"^168\.63\.129\.16$", "cloud metadata", "infra"),
    (r"^localhost$", "local resolver", "infra"),
    (r"^ip6-allrouters$", "local network", "infra"),
    (r"^\d+\.\d+\.\d+\.\d+$", "raw ip", "review"),
    (r".*\.github\.com$", "github infra", "infra"),
]
INFRA_PROCS = re.compile(r"hosted-compute|systemd|python3", re.I)
SUSPICIOUS_PROCS = re.compile(
    r"(curl|wget).*(sh|bash)|chmod\s+\+x|/tmp/.*(exec|run)|\.sh\s*$", re.I
)


def gh(method, path, fields=None):
    cmd = ["gh", "api", "-X", method, path]
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {method} {path}: {r.stderr[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def classify(domain, chain=()):
    if any(INFRA_PROCS.search(p) for p in chain):
        return "runner infra", "infra"
    d = domain.replace("[.]", ".")
    for pat, cat, verdict in DEST_RULES:
        if re.match(pat, d):
            return cat, verdict
    return "unknown", "review"


def parse_profile(body):
    m = re.search(r"garnet:summary (\{.*?\}) -->", body)
    summary = json.loads(m.group(1)) if m else {}
    pre = re.search(r"<pre>(.*?)</pre>", body, re.S)
    tree = pre.group(1) if pre else ""
    dests, stack, step_ctx = [], [], ""
    for line in tree.split("\n"):
        sm = re.search(r"\(step:\s*&quot;([^&]+)&quot;\)", line)
        if sm:
            step_ctx = sm.group(1)
        pm = re.search(r"<strong>([^<]+)</strong>", line)
        if pm and "○" not in line:
            indent = len(line) - len(line.lstrip(" │├└─"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, pm.group(1)))
        dm = re.search(r"○\s+([a-z0-9\.\-\[\]]+)", line)
        if dm:
            dests.append({
                "domain": dm.group(1).replace("[.]", "."),
                "chain": [p for _, p in stack],
                "step": step_ctx,
            })
    seen, uniq = set(), []
    for d in dests:
        if d["domain"] not in seen:
            seen.add(d["domain"])
            uniq.append(d)
    return {"summary": summary, "destinations": uniq}


def pr_type(owner, repo, pr):
    files = gh("GET", f"/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100")
    names = [f["filename"] for f in files]
    if not names:
        return "empty"
    if all(re.search(
        r"(package\.json|pnpm-lock\.yaml|yarn\.lock|package-lock\.json|pnpm-workspace\.yaml)$", n
    ) for n in names):
        return "dependency"
    if all("test" in n or "e2e" in n for n in names):
        return "test"
    return "general"


def evaluate(profile, ptype):
    findings = []
    dests, summary = profile["destinations"], profile["summary"]
    if not dests:
        return [("hold", "No execution destinations recorded — nothing to judge.")]
    for d in dests:
        cat, verdict = classify(d["domain"], d["chain"])
        chain = " → ".join(d["chain"][-3:]) if d["chain"] else "?"
        if verdict == "review":
            findings.append(("hold",
                f"Unknown destination `{d['domain']}` ({cat}) reached via {chain}"
                + (f" during step \"{d['step']}\"" if d["step"] else "") + "."))
    for d in dests:
        if SUSPICIOUS_PROCS.search(" ".join(d["chain"])):
            findings.append(("hold",
                f"Suspicious execution pattern in chain: {' '.join(d['chain'])}."))
    return findings


def decide(findings, ptype):
    holds = [t for s, t in findings if s == "hold"]
    if ptype != "dependency":
        return "AUDIT", holds, []
    return ("HOLD", holds, []) if holds else ("MERGE", [], [])


def render(profile, ptype, decision, holds, elapsed):
    s = profile["summary"]
    lines = [MARKER, f"## Merge gate (POC): **{decision}**", "",
        f"Evaluated the Garnet execution profile for this PR "
        f"({s.get('chains', '?')} chains, {s.get('destinations', '?')} destinations, "
        f"recorded {s.get('recorded', '?')}) in {elapsed:.1f}s.",
        f"PR type inferred from changed files: **{ptype}**.", ""]
    if decision == "MERGE":
        lines += ["Every observed destination classifies as expected package/CI "
                  "infrastructure. Nothing in the execution behavior contradicts the diff.", ""]
    elif decision == "AUDIT":
        lines += ["v1 policy is scoped to dependency PRs, so this is an audit, not a verdict. "
                  "Destinations outside the dependency allowlist:", ""]
        lines += [f"- {h}" for h in holds] + [""]
    else:
        lines += ["### Findings requiring review", ""]
        lines += [f"- {h}" for h in holds] + [""]
    lines += ["_This is a proof-of-concept gate, not a product verdict. "
              "Garnet reports; the reviewer decides._"]
    return "\n".join(lines)


def main():
    owner_repo, pr = sys.argv[1], sys.argv[2]
    owner, repo = owner_repo.split("/")
    t0 = time.time()
    comments = gh("GET", f"/repos/{owner}/{repo}/issues/{pr}/comments?per_page=30")
    if any(MARKER in c["body"] for c in comments):
        print("gate comment already exists; skipping")
        return
    reviews = [c["body"] for c in comments if c["user"]["login"] == BOT]
    if not reviews:
        print("no Runtime Review comment found; skipping")
        return
    profile = parse_profile(reviews[-1])
    ptype = pr_type(owner, repo, pr)
    decision, holds, _ = decide(evaluate(profile, ptype), ptype)
    body = render(profile, ptype, decision, holds, time.time() - t0)
    gh("POST", f"/repos/{owner}/{repo}/issues/{pr}/comments", {"body": body})
    print(f"posted {decision} for {owner_repo}#{pr}")


if __name__ == "__main__":
    main()
