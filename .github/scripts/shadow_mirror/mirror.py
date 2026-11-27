"""Replay upstream dependencies with isolated Garnet recording."""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FORK = "garnet-labs/sentry-javascript"
SOURCE = "getsentry/sentry-javascript"
BRANCH = "develop"
LOOKBACK_DAYS = 10
WORKFLOW = "garnet-record.yml"
MARKER = re.compile(
    r"<!-- garnet-shadow-mirror upstream=(\d+) upstream_head=([a-f0-9]{40}) "
    r"upstream_base=([a-f0-9]{40}) -->"
)
DEPENDENCY_BOTS = {"dependabot[bot]": 49699333, "renovate[bot]": 29139614}
ROOT_INPUTS = {"package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock", ".npmrc"}
WORKSPACE_ROOTS = {"packages", "dev-packages"}
CI_SKIP = re.compile(
    r"\[(skip ci|ci skip|no ci|skip actions|actions skip)\]", re.IGNORECASE
)
DISCOVERY = """
query ($search: String!, $cursor: String) {
  search(query: $search, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number } }
  }
}
"""
class APIError(RuntimeError):
    def __init__(self, method, path, status, detail):
        super().__init__(f"{method} {path}: HTTP {status}: {detail}")
        self.status = status


class API:
    def __init__(self, token, dry_run):
        self.token = token
        self.dry_run = dry_run

    def call(self, repo, path, method="GET", data=None):
        if method != "GET" and (self.dry_run or repo != FORK):
            raise ValueError("Only live writes to the mirror repository are allowed")
        return self._request(f"repos/{repo}/{path}", method, data)

    def _request(self, path, method, data):
        request = urllib.request.Request(
            f"https://api.github.com/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise APIError(method, path, error.code, error.read().decode()) from error

    def pages(self, repo, path):
        separator = "&" if "?" in path else "?"
        for page in range(1, 1000):
            items = self.call(repo, f"{path}{separator}per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return
        raise RuntimeError(f"Pagination limit reached: {path}")

    def write(self, path, data, method="POST"):
        return self.call(FORK, path, method, data)

    def dependency_candidates(self, now=None):
        since = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(
            days=LOOKBACK_DAYS
        )
        authors = " ".join(
            f"author:app/{login.removesuffix('[bot]')}" for login in DEPENDENCY_BOTS
        )
        for scope in ("is:open", f"is:merged merged:>={since:%Y-%m-%dT%H:%M:%SZ}"):
            cursor = None
            while True:
                search = f"repo:{SOURCE} is:pr {scope} {authors}"
                result = self._request(
                    "graphql",
                    "POST",
                    {"query": DISCOVERY, "variables": {"search": search, "cursor": cursor}},
                )
                if result.get("errors"):
                    raise RuntimeError(f'GitHub discovery: {result["errors"]}')
                page = result["data"]["search"]
                yield from (pr["number"] for pr in page["nodes"] if pr)
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]


def dependency_file(path):
    parts = path.split("/")
    return path in ROOT_INPUTS or (
        len(parts) == 3 and parts[0] in WORKSPACE_ROOTS and parts[2] == "package.json"
    )


def dependency_bot(pr):
    author = pr.get("user") or {}
    return (
        DEPENDENCY_BOTS.get(author.get("login")) == author.get("id")
        and author.get("type") == "Bot"
        and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == SOURCE
    )


def is_dependency_pr(pr, files):
    if not files or pr["changed_files"] != len(files) or not dependency_bot(pr):
        return False
    return any(
        dependency_file(file["filename"])
        or dependency_file(file.get("previous_filename", ""))
        for file in files
    )


def within_lookback(timestamp, now=None):
    if not timestamp:
        return False
    moment = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now - moment <= datetime.timedelta(days=LOOKBACK_DAYS)


def recently_merged(pr, now=None, history=None):
    if not pr.get("merged_at"):
        return False
    return within_lookback(pr["merged_at"], now) or any(
        mirror["state"] == "open" and within_lookback(mirror.get("created_at"), now)
        for mirror in (history or {}).values()
    )


def scrub_title(title):
    title = re.sub(r"https?://\S+|www\.\S+|#\d+", "", title)
    title = re.sub(re.escape(SOURCE), "", title, flags=re.IGNORECASE)
    title = re.sub(r"(?<![\w/])@[\w.-]+(?![\w./-])", "", title)
    title = " ".join(title.split())
    while CI_SKIP.search(title):
        title = " ".join(CI_SKIP.sub("", title).split())
    return title[:256].rstrip() or "Dependency update"


def recording_workflow(content):
    content, count = re.subn(
        r"(?m)^(    branches: \['develop', 'record-base/\*\*')\]$",
        r"\1, 'shadow/base/**']",
        content,
    )
    if count != 1:
        raise ValueError("Unexpected Garnet recording push branch configuration")
    content, count = re.subn(
        r"(?m)^  pull_request:$", "  pull_request:\n  workflow_dispatch:", content
    )
    content, jobs = re.subn(
        r"(?m)^(    if: github\.event_name )== 'push'( \|\| )", r"\1!= 'pull_request'\2", content
    )
    if count != 1 or jobs != 1:
        raise ValueError("Unexpected Garnet recording trigger configuration")
    checkouts = len(re.findall(r"uses: actions/checkout@", content))
    if not checkouts or checkouts != len(
        re.findall(r"(?m)^          persist-credentials: false$", content)
    ):
        raise ValueError("Unexpected Garnet recording checkout configuration")
    return content


def mirror_revision(pr, number):
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if any((side.get("repo") or {}).get("full_name") != FORK for side in (head, base)):
        return None
    match = re.fullmatch(
        rf"shadow/pr/{number}(-r([2-9]|[1-9]\d+))?", head.get("ref", "")
    )
    if match and base.get("ref") == f"shadow/base/{number}{match[1] or ''}":
        return int(match[2] or 1)
    return None


class Mirror:
    def __init__(self, api):
        self.api = api
        self.mirrors = {}
        for pr in api.pages(FORK, "pulls?state=all"):
            marker = MARKER.search(pr.get("body") or "")
            if not marker:
                continue
            number = int(marker[1])
            revision = mirror_revision(pr, number)
            if revision is not None:
                self.mirrors.setdefault(number, {})[revision] = pr
        self.workflow = None

    def sync(self, number):
        api = self.api
        history = self.mirrors.get(number, {})
        path = f"pulls/{number}"
        try:
            pr = api.call(SOURCE, path)
            live = pr["state"] == "open" or recently_merged(pr, history=history)
            if live:
                files = list(api.pages(SOURCE, f"{path}/files"))
                current = api.call(SOURCE, path)
        except APIError as error:
            if error.status != 404:
                raise
            return {"status": "skipped", "reason": "upstream PR not found"}
        if not live:
            return self.close(history, "source closed")
        if any(current[key] != pr[key] for key in ("state", "changed_files")) or any(
            current[key]["sha"] != pr[key]["sha"] for key in ("base", "head")
        ):
            return {"status": "source changed during discovery"}
        if pr["changed_files"] != len(files):
            return {"status": "incomplete file list"}
        if not is_dependency_pr(pr, files):
            return self.close(history, "not an eligible recorded dependency change")
        upstream_head = pr["head"]["sha"]
        upstream_base = api.call(
            SOURCE, f'compare/{pr["base"]["sha"]}...{upstream_head}'
        )["merge_base_commit"]["sha"]
        if api.dry_run:
            return {
                "status": "dry-run",
                "upstream_base": upstream_base,
                "upstream_head": upstream_head,
                "files": [file["filename"] for file in files],
            }
        return self.replay(number, pr, files, upstream_base, history)

    def close(self, history, reason, keep=None):
        for pr in history.values():
            if pr["state"] == "open" and pr["number"] != keep and not self.api.dry_run:
                self.api.write(f'pulls/{pr["number"]}', {"state": "closed"}, "PATCH")
        return {"status": "skipped", "reason": reason}

    def replay(self, number, pr, files, upstream_base, history):
        api = self.api
        upstream_head = pr["head"]["sha"]
        if self.workflow is None:
            data = api.call(FORK, f"contents/.github/workflows/{WORKFLOW}?ref={BRANCH}")
            self.workflow = recording_workflow(
                base64.b64decode(data["content"]).decode()
            )
        last = max(history, default=0)
        revision = max(1, last)
        fingerprint = hashlib.sha256(self.workflow.encode()).hexdigest()
        baseline_message = f"chore: shadow baseline\n\nSource tree at {upstream_base}; recorder {fingerprint}"
        while True:
            suffix = f"-r{revision}" if revision > 1 else ""
            base_ref = f"shadow/base/{number}{suffix}"
            head_ref = f"shadow/pr/{number}{suffix}"
            existing = history.get(revision)
            baseline = self.get_ref(base_ref)
            if (existing and (existing["state"] != "open" or not baseline)) or (
                baseline
                and api.call(FORK, f"git/commits/{baseline}")["message"]
                != baseline_message
            ):
                revision += 1
            else:
                break

        if existing and MARKER.search(existing["body"]).groups()[1:] == (
            upstream_head,
            upstream_base,
        ):
            self.close(history, "superseded", keep=existing["number"])
            return {
                "status": "unchanged",
                "url": existing["html_url"],
            }

        if not baseline:
            baseline = self.commit(self.tree(upstream_base), [], baseline_message)
            self.create_ref(base_ref, baseline)
        if not self.baseline_started(base_ref, baseline):
            return {"status": "baseline pending", "base_ref": base_ref}

        parent = self.get_ref(head_ref)
        tree = self.tree(upstream_head)
        title = scrub_title(pr["title"])
        if parent and api.call(FORK, f"git/commits/{parent}")["tree"]["sha"] == tree:
            head = parent
        else:
            head = self.commit(tree, [parent or baseline], title)
            if parent:
                api.write(
                    f"git/refs/heads/{head_ref}", {"sha": head, "force": False}, "PATCH"
                )
            else:
                self.create_ref(head_ref, head)

        names = (
            "    "
            + json.dumps(file["filename"], ensure_ascii=False).replace("<", r"\u003c")
            for file in files
        )
        body = "\n".join(
            [
                "Source files changed (only the Garnet recorder is retained under `.github/workflows`):",
                "",
                *names,
                "",
                f"Mirror base `{baseline}` → head `{head}`",
                "",
                f"<!-- garnet-shadow-mirror upstream={number} upstream_head={upstream_head} upstream_base={upstream_base} -->",
            ]
        )
        data = {"title": title, "body": body}
        if existing:
            result = api.write(f'pulls/{existing["number"]}', data, "PATCH")
        else:
            result = api.write(
                "pulls", {**data, "head": head_ref, "base": base_ref}
            )
        self.close(history, "superseded", keep=result["number"])
        return {
            "status": "updated" if existing else "created",
            "url": result["html_url"],
        }

    def get_ref(self, name):
        try:
            return self.api.call(FORK, f"git/ref/heads/{name}")["object"]["sha"]
        except APIError as error:
            if error.status != 404:
                raise
            return None

    def create_ref(self, name, sha):
        self.api.write("git/refs", {"ref": f"refs/heads/{name}", "sha": sha})

    def baseline_started(self, branch, sha):
        query = urllib.parse.urlencode({"branch": branch, "head_sha": sha})
        path = f"actions/workflows/{WORKFLOW}/runs?{query}"
        for attempt in range(12):
            if attempt:
                time.sleep(5)
            if self.api.call(FORK, path)["workflow_runs"]:
                return True
            if attempt == 5:
                self.api.write(
                    f"actions/workflows/{WORKFLOW}/dispatches", {"ref": branch}
                )
        return False

    def tree(self, sha):
        api = self.api
        root = api.call(FORK, f"git/commits/{sha}")["tree"]["sha"]
        entries = api.call(FORK, f"git/trees/{root}")["tree"]
        github = next(
            (
                e["sha"]
                for e in entries
                if e["path"] == ".github" and e["type"] == "tree"
            ),
            None,
        )
        workflows = api.write(
            "git/trees",
            {
                "tree": [
                    {
                        "path": WORKFLOW,
                        "mode": "100644",
                        "type": "blob",
                        "content": self.workflow,
                    }
                ]
            },
        )["sha"]
        replacement = {
            "tree": [
                {
                    "path": "workflows",
                    "mode": "040000",
                    "type": "tree",
                    "sha": workflows,
                }
            ]
        }
        if github:
            replacement["base_tree"] = github
        github = api.write("git/trees", replacement)["sha"]
        return api.write(
            "git/trees",
            {
                "base_tree": root,
                "tree": [
                    {"path": ".github", "mode": "040000", "type": "tree", "sha": github},
                ],
            },
        )["sha"]

    def commit(self, tree, parents, message):
        return self.api.write(
            "git/commits", {"tree": tree, "parents": parents, "message": message}
        )["sha"]

def main():
    global LOOKBACK_DAYS
    parser = argparse.ArgumentParser()
    sync = parser.add_subparsers(dest="command", required=True).add_parser("sync")
    sync.add_argument("--pr", type=int, action="append")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    args = parser.parse_args()
    LOOKBACK_DAYS = args.lookback_days
    token = os.environ.get("SHADOW_MIRROR_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        parser.error("Set SHADOW_MIRROR_TOKEN or GH_TOKEN")
    api = API(token, args.dry_run)
    mirror = Mirror(api)
    numbers = args.pr
    if not numbers:
        numbers = list(api.dependency_candidates())
        numbers.extend(
            number
            for number, history in mirror.mirrors.items()
            if any(pr["state"] == "open" for pr in history.values())
        )
    for number in dict.fromkeys(numbers):
        try:
            result = mirror.sync(number)
        except (APIError, OSError, ValueError) as error:
            sys.stdout.write(
                json.dumps({"pr": number, "status": "error", "error": str(error)})
                + "\n"
            )
            return 1
        sys.stdout.write(json.dumps({"pr": number, **result}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
