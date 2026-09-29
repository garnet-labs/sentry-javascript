#!/usr/bin/env python3
"""merge-gate POC: merge-safety decisions on PRs driven by Garnet execution behavior.

Thesis: a reviewer can make a merge / no-merge call on a dependency PR from its
Garnet execution profile -- at the PR gate, in seconds, with legible reasons --
catching what the diff alone cannot show.

Usage:
  gate.py <owner/repo> <pr_number> [--post] [--json]
  gate.py --synthetic   # prove the HOLD path fires on a malicious profile

Reads the Garnet Runtime Review comment on the PR, parses the execution profile,
evaluates v1 merge-safety assertions, renders a decision. With --post, posts the
decision as a PR comment (the gate demonstration).

v1 policy scope: dependency PRs (manifest/lockfile-only changes). Other PR types
run in audit mode.
"""
import json
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request

API = "https://api.github.com"
GH_API = "/home/hatch/workspace/skills/github/bin/gh-api"

# ----------------------------------------------------------------------------
# Destination classification. Tiers for a dependency-install execution profile.
# ----------------------------------------------------------------------------
# (domain pattern, category, verdict)
# verdict: ok = expected, infra = runner background noise, review = flag it
DEST_RULES = [
    # package + toolchain infra: the normal footprint of an install
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
    # CI runner / OS background noise
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

SUSPICIOUS_PROCS = re.compile(
    r"(curl|wget).*(sh|bash)|chmod\s+\+x|/tmp/.*(exec|run)|\.sh\s*$", re.I
)


def gh(method, path, body=None):
    cmd = [GH_API, method, path]
    if body is not None:
        cmd.append(json.dumps(body))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh-api {method} {path} failed: {r.stderr[:300]}")
    return json.loads(r.stdout)


INFRA_PROCS = re.compile(r"hosted-compute|systemd|python3", re.I)


def classify(domain, chain=()):
    # runner-infra processes make any destination background noise
    if any(INFRA_PROCS.search(p) for p in chain):
        return "runner infra", "infra"
    d = domain.replace("[.]", ".")
    for pat, cat, verdict in DEST_RULES:
        if re.match(pat, d):
            return cat, verdict
    return "unknown", "review"


class ProfileNotReady(Exception):
    """The Runtime Review comment exists but its garnet:summary payload is
    missing or unparseable -- the bot may still be finalizing the comment."""


def parse_profile(comment_body):
    """Extract summary JSON + destination chains from the Runtime Review comment.

    Raises ProfileNotReady when the garnet:summary payload is missing or
    unparseable (e.g. the bot posts the comment first and edits the summary
    in later). Callers must surface that state loudly -- never render
    placeholder values in its place.
    """
    m = re.search(r"garnet:summary (\{.*?\}) -->", comment_body)
    if not m:
        raise ProfileNotReady("no garnet:summary payload in Runtime Review comment")
    try:
        summary = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ProfileNotReady(f"garnet:summary payload is not valid JSON: {e}")
    for key in ("chains", "destinations", "recorded"):
        if key not in summary:
            raise ProfileNotReady(f"garnet:summary payload missing {key!r}")
    pre = re.search(r"<pre>(.*?)</pre>", comment_body, re.S)
    tree = pre.group(1) if pre else ""
    # walk the tree: track process ancestry, capture (domain, chain, step)
    dests = []
    # split into lines, track indent-based ancestry of <strong> procs
    stack = []  # (indent, proc)
    step_ctx = ""
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
            chain = [p for _, p in stack]
            dests.append({
                "domain": dm.group(1).replace("[.]", "."),
                "chain": chain,
                "step": step_ctx,
            })
    # dedupe by domain, keep first chain
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
        return "empty", names
    depish = all(
        re.search(r"(package\.json|pnpm-lock\.yaml|yarn\.lock|package-lock\.json|pnpm-workspace\.yaml)$", n)
        for n in names
    )
    if depish:
        return "dependency", names
    testish = all("test" in n or "e2e" in n for n in names)
    if testish:
        return "test", names
    return "general", names


def evaluate(profile, ptype):
    findings = []  # (severity, text)
    dests = profile["destinations"]
    summary = profile["summary"]

    if not dests:
        findings.append(("hold", "No execution destinations recorded — nothing to judge."))
        return findings
    if summary.get("contract", "").split(".")[0] != "6":
        findings.append(("note", f"Unrecognized contract {summary.get('contract')}."))

    for d in profile["destinations"]:
        cat, verdict = classify(d["domain"], d["chain"])
        chain = " → ".join(d["chain"][-3:]) if d["chain"] else "?"
        if verdict == "review":
            findings.append((
                "hold",
                f"Unknown destination `{d['domain']}` ({cat}) reached via {chain}"
                + (f" during step \"{d['step']}\"" if d["step"] else "") + ".",
            ))
        elif verdict == "ok" and ptype == "dependency":
            pass  # expected footprint
        elif verdict == "infra":
            pass  # background noise

    # suspicious execution patterns in chains
    for d in profile["destinations"]:
        chain_s = " ".join(d["chain"])
        if SUSPICIOUS_PROCS.search(chain_s):
            findings.append(("hold", f"Suspicious execution pattern in chain: {chain_s}."))

    if ptype != "dependency":
        findings.append((
            "note",
            f"PR type is '{ptype}', not dependency — v1 policy is tuned for dependency "
            "installs; treat this as an audit, not a gate verdict.",
        ))
    return findings


def decide(findings, ptype):
    holds = [f for f in findings if f[0] == "hold"]
    notes = [f for f in findings if f[0] == "note"]
    if ptype != "dependency":
        # v1 policy only has gate authority for dependency PRs; everything
        # else is an audit with observations, never a HOLD.
        return "AUDIT", [t for s, t in findings if s == "hold"], notes
    if holds:
        return "HOLD", holds, notes
    return "MERGE", [], notes


def render(owner, repo, pr, profile, ptype, decision, holds, notes, elapsed_s):
    s = profile["summary"]
    # Loud failure, not placeholders: a render without real numbers is worse
    # than no render. parse_profile already validates, this is defense in depth.
    for key in ("chains", "destinations", "recorded"):
        if key not in s:
            raise ValueError(f"refusing to render merge gate: summary missing {key!r}")
    lines = [
        "<!-- merge-gate-poc -->",
        f"## Merge gate (POC): **{decision}**",
        "",
        f"Evaluated the Garnet execution profile for this PR "
        f"({s['chains']} chains, {s['destinations']} destinations, "
        f"recorded {s['recorded']}) in {elapsed_s:.1f}s.",
        f"PR type inferred from changed files: **{ptype}**.",
        "",
    ]
    if decision == "MERGE":
        lines += [
            "Every observed destination classifies as expected package/CI infrastructure. "
            "Nothing in the execution behavior contradicts the diff.",
            "",
        ]
    elif decision == "AUDIT":
        lines += [
            "v1 policy is scoped to dependency PRs, so this is an audit, not a verdict.",
            "",
        ]
        if holds:
            lines += ["Destinations outside the dependency allowlist:", ""]
            for text in holds:
                lines.append(f"- {text}")
        else:
            lines += [
                "Nothing in the recorded execution contradicts the diff.",
            ]
        lines.append("")
    else:
        lines += ["### Findings requiring review", ""]
        for _, text in holds:
            lines.append(f"- {text}")
        lines.append("")
    if notes:
        for _, text in notes:
            lines.append(f"_Note: {text}_")
        lines.append("")
    lines += [
        "_This is a proof-of-concept gate, not a product verdict. "
        "Garnet reports; the reviewer decides._",
    ]
    return "\n".join(lines)


def run_gate(owner, repo, pr, post=False, retries=3, retry_delay_s=20):
    import time
    t0 = time.time()
    profile = None
    last_err = None
    for attempt in range(retries):
        comments = gh("GET", f"/repos/{owner}/{repo}/issues/{pr}/comments?per_page=30")
        bodies = [c["body"] for c in comments if c["user"]["login"] == "garnet-runtime-review[bot]"]
        if not bodies:
            return {"error": "no Garnet Runtime Review comment found on this PR"}
        try:
            profile = parse_profile(bodies[-1])
            break
        except ProfileNotReady as e:
            # The bot posts the comment first and edits the summary payload in
            # later (seen: +12 min on sentry-javascript#4). Wait briefly, then
            # fail loudly -- never render placeholder numbers.
            last_err = e
            if attempt < retries - 1:
                time.sleep(retry_delay_s)
    if profile is None:
        return {"error": f"Runtime Review comment has no usable execution summary yet ({last_err}). "
                         "The bot may still be finalizing it -- re-run /merge-gate once it is."}
    ptype, files = pr_type(owner, repo, pr)
    findings = evaluate(profile, ptype)
    decision, holds, notes = decide(findings, ptype)
    elapsed = time.time() - t0
    md = render(owner, repo, pr, profile, ptype, decision, holds, notes, elapsed)
    result = {
        "pr": f"{owner}/{repo}#{pr}",
        "pr_type": ptype,
        "decision": decision,
        "holds": [t for _, t in holds],
        "notes": [t for _, t in notes],
        "chains": profile["summary"].get("chains"),
        "destinations": profile["summary"].get("destinations"),
        "elapsed_s": round(elapsed, 1),
        "comment": md,
    }
    if post:
        r = gh("POST", f"/repos/{owner}/{repo}/issues/{pr}/comments", {"body": md})
        result["posted"] = r["html_url"]
    return result


def synthetic():
    """Prove the HOLD path fires: real posthog profile + injected C2 destination."""
    comments = gh("GET", "/repos/garnet-labs/posthog/issues/209/comments?per_page=30")
    bodies = [c["body"] for c in comments if c["user"]["login"] == "garnet-runtime-review[bot]"]
    profile = parse_profile(bodies[-1])
    profile["destinations"].append({
        "domain": "metrics-collector.evil-example.net",
        "chain": ["bash", "node", "dash", "curl"],
        "step": "Install dependencies (lifecycle scripts execute here)",
    })
    findings = evaluate(profile, "dependency")
    decision, holds, notes = decide(findings, "dependency")
    return {
        "case": "synthetic: dependency install exfiltrating to unknown domain",
        "decision": decision,
        "holds": [t for _, t in holds],
    }


def main(argv):
    if "--synthetic" in argv:
        print(json.dumps(synthetic(), indent=2))
        return
    owner_repo, pr = argv[1], argv[2]
    owner, repo = owner_repo.split("/")
    post = "--post" in argv
    out = run_gate(owner, repo, pr, post=post)
    if post:
        print(out.get("posted", "post failed"))
    else:
        print(json.dumps({k: v for k, v in out.items() if k != "comment"}, indent=2))


if __name__ == "__main__":
    main(sys.argv)
