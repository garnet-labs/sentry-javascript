import base64
import datetime
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from mirror import (
    API,
    FORK,
    GATE_MARKER,
    HOLD,
    HOLD_MARKER,
    MARK_READY,
    SOURCE,
    WORKFLOW,
    APIError,
    Mirror,
    is_dependency_pr,
    recently_merged,
    main,
    mirror_revision,
    recording_workflow,
    scrub_title,
)


DEPENDABOT = {"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}
REAL_ADAPTERS = Mirror.reviewer_adapters
UPSTREAM_HEAD = {"repo": {"full_name": SOURCE}}


class DiscoveryTest(unittest.TestCase):
    def test_installed_workspace_inputs_qualify(self):
        for path in (
            "package.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "packages/nextjs/package.json",
            "dev-packages/rollup-utils/package.json",
        ):
            with self.subTest(path=path):
                pr = {"changed_files": 1, "user": DEPENDABOT, "head": UPSTREAM_HEAD}
                self.assertTrue(is_dependency_pr(pr, [{"filename": path}]))

    def test_unrecorded_inputs_do_not_qualify(self):
        for path in (
            "dev-packages/e2e-tests/test-applications/nextjs-15/package.json",
            "dev-packages/e2e-tests/test-applications/nextjs-15/pnpm-lock.yaml",
            ".github/workflows/build.yml",
            "packages/nextjs/src/index.ts",
        ):
            with self.subTest(path=path):
                pr = {"changed_files": 1, "user": DEPENDABOT, "head": UPSTREAM_HEAD}
                self.assertFalse(is_dependency_pr(pr, [{"filename": path}]))

    def test_only_named_upstream_bots_qualify(self):
        files = [{"filename": "README.md"}, {"filename": "pnpm-lock.yaml"}]
        for user, head, qualified in (
            (DEPENDABOT, UPSTREAM_HEAD, True),
            ({"login": "renovate[bot]", "id": 29139614, "type": "Bot"}, UPSTREAM_HEAD, True),
            ({"login": "dependabot[bot]", "id": 1, "type": "Bot"}, UPSTREAM_HEAD, False),
            ({"login": "dependabot[bot]", "id": 49699333, "type": "User"}, UPSTREAM_HEAD, False),
            ({"login": "developer", "id": 2, "type": "User"}, UPSTREAM_HEAD, False),
            (DEPENDABOT, {"repo": {"full_name": "someone/fork"}}, False),
        ):
            with self.subTest(user=user, head=head):
                pr = {"changed_files": 2, "user": user, "head": head}
                self.assertEqual(is_dependency_pr(pr, files), qualified)
        pr = {"changed_files": 2, "user": DEPENDABOT, "head": UPSTREAM_HEAD}
        self.assertFalse(is_dependency_pr(pr, files[:1]))

    def test_recently_merged_uses_lookback(self):
        now = datetime.datetime(2026, 11, 27, tzinfo=datetime.timezone.utc)
        self.assertTrue(recently_merged({"merged_at": "2026-11-18T00:00:00Z"}, now))
        self.assertFalse(recently_merged({"merged_at": "2026-11-16T00:00:00Z"}, now))
        self.assertFalse(recently_merged({"merged_at": None}, now))

    def test_candidates_cover_open_and_recently_merged_bot_changes(self):
        api = API("unused", True)

        def page(numbers, cursor=None):
            return {
                "data": {
                    "search": {
                        "nodes": [{"number": n} for n in numbers] + [{}],
                        "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
                    }
                }
            }

        api._request = Mock(side_effect=[page([1], "next"), page([2]), page([3])])
        now = datetime.datetime(2026, 11, 27, tzinfo=datetime.timezone.utc)
        self.assertEqual(list(api.dependency_candidates(now)), [1, 2, 3])
        searches = [c.args[-1]["variables"]["search"] for c in api._request.call_args_list]
        self.assertTrue(all(f"repo:{SOURCE} is:pr" in q for q in searches))
        self.assertTrue(all("author:app/dependabot author:app/renovate" in q for q in searches))
        self.assertIn("is:open", searches[0])
        self.assertEqual(api._request.call_args_list[1].args[-1]["variables"]["cursor"], "next")
        self.assertIn("is:merged merged:>=2026-11-17T00:00:00Z", searches[2])

    def test_upstream_and_dry_run_writes_are_blocked(self):
        for api, repo in ((API("unused", False), SOURCE), (API("unused", True), FORK)):
            with self.assertRaisesRegex(ValueError, "live writes to the mirror"):
                api.call(repo, "pulls", "POST", {})

    def test_ready_mutation_only_accepts_live_mirror_pairs(self):
        pr = {
            "node_id": "PR_node",
            "base": {"ref": "shadow/base/1", "repo": {"full_name": FORK}},
            "head": {"ref": "shadow/pr/1", "repo": {"full_name": FORK}},
        }
        api = API("unused", True)
        api._request = Mock()
        with self.assertRaisesRegex(ValueError, "live writes to the mirror"):
            api.mark_ready(pr, 1)
        api.dry_run = False
        with self.assertRaisesRegex(ValueError, "live writes to the mirror"):
            api.mark_ready(
                {**pr, "head": {**pr["head"], "repo": {"full_name": SOURCE}}}, 1
            )
        api._request.assert_not_called()
        api._request.return_value = {
            "data": {
                "markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}
            }
        }
        api.mark_ready(pr, 1)
        api._request.assert_called_once_with(
            "graphql", "POST", {"query": MARK_READY, "variables": {"id": "PR_node"}}
        )
        api._request.return_value = {"errors": [{"message": "permission denied"}]}
        with self.assertRaisesRegex(ValueError, "permission denied"):
            api.mark_ready(pr, 1)

    def test_scrub_title_removes_upstream_references(self):
        self.assertEqual(
            scrub_title(
                f"Bump @scope/name [skip ci] {SOURCE}#123 @person https://github.com/{SOURCE}/pull/123"
            ),
            "Bump @scope/name",
        )

    def test_scrub_title_cannot_recreate_ci_skip_directives(self):
        for directive in (
            "[skip  ci]",
            "[ci\tskip]",
            "[No\nCI]",
            "[skip @user actions]",
            "[actions [skip ci] skip]",
        ):
            with self.subTest(directive=directive):
                self.assertEqual(
                    scrub_title(f"Bump fastapi {directive}"), "Bump fastapi"
                )

    def test_mirror_revision_requires_exact_same_repo_pair(self):
        pr = {
            "base": {"repo": {"full_name": FORK}, "ref": "shadow/base/1-r2"},
            "head": {"repo": {"full_name": FORK}, "ref": "shadow/pr/1-r2"},
        }
        self.assertEqual(mirror_revision(pr, 1), 2)
        self.assertIsNone(mirror_revision(pr, 2))
        self.assertIsNone(
            mirror_revision({**pr, "head": {**pr["head"], "ref": "shadow/pr/1"}}, 1)
        )
        pr["head"]["repo"]["full_name"] = SOURCE
        self.assertIsNone(mirror_revision(pr, 1))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.api = Mock(dry_run=False)
        self.api.pages.return_value = []
        self.mirror = Mirror(self.api)
        self.api.reset_mock()
        self.source_base = "a" * 40
        self.merge_base = "b" * 40
        self.source_head = "c" * 40
        self.pr = {
            "state": "open",
            "title": "Bump fastapi",
            "changed_files": 1,
            "user": DEPENDABOT,
            "base": {"sha": self.source_base},
            "head": {"sha": self.source_head, "repo": {"full_name": SOURCE}},
        }
        self.files = [{"filename": "pnpm-lock.yaml"}]
        adapters = patch.object(Mirror, "reviewer_adapters", return_value=[])
        adapters.start()
        self.addCleanup(adapters.stop)
        self.workflow_path = (
            Path(__file__).resolve().parents[2] / "workflows" / WORKFLOW
        )

    def test_baseline_uses_merge_base_and_head_uses_original_head(self):
        def read(repo, path):
            if path == "pulls/1":
                return self.pr
            if path == f"compare/{self.source_base}...{self.source_head}":
                return {"merge_base_commit": {"sha": self.merge_base}}
            if path.startswith("contents/"):
                return {
                    "content": base64.b64encode(
                        self.workflow_path.read_bytes()
                    ).decode()
                }
            if path.startswith("git/ref/"):
                raise APIError("GET", path, 404, "missing ref")
            if path.startswith("actions/"):
                return {"workflow_runs": [{"id": 1}]}
            raise AssertionError((repo, path))

        self.api.call.side_effect = read
        self.pr.update(changed_files=2)
        self.files.append(
            {"filename": "<!-- garnet:record-summary:opt-in -->/package.json"}
        )
        self.api.pages.side_effect = [self.files, []]
        self.api.write.side_effect = [
            {"sha": "baseline"},
            {},
            {"sha": "head"},
            {},
            {"number": 2, "html_url": "https://github.com/garnet-labs/sentry-javascript/pull/2"},
            {},
        ]
        with patch.object(
            self.mirror, "tree", side_effect=["base-tree", "head-tree"]
        ) as tree:
            self.assertEqual(self.mirror.sync(1)["status"], "created")
        self.assertEqual(
            tree.call_args_list, [call(self.merge_base), call(self.source_head)]
        )
        writes = self.api.write.call_args_list
        self.assertEqual(
            [write.args[0] for write in writes],
            [
                "git/commits",
                "git/refs",
                "git/commits",
                "git/refs",
                "pulls",
                "issues/2/comments",
            ],
        )
        self.assertEqual(writes[0].args[1]["parents"], [])
        self.assertEqual(writes[2].args[1]["parents"], ["baseline"])
        self.assertEqual(writes[4].args[1]["base"], "shadow/base/1")
        self.assertEqual(writes[4].args[1]["head"], "shadow/pr/1")
        self.assertIs(writes[4].args[1]["draft"], True)
        self.assertNotIn(
            "<!-- garnet:record-summary:opt-in -->", writes[4].args[1]["body"]
        )
        self.assertEqual(writes[5], call("issues/2/comments", {"body": HOLD}))
        self.api.mark_ready.assert_not_called()

    def test_later_sync_promotes_only_with_trusted_finalized_current_head(self):
        head = "d" * 40
        existing = {
            "number": 2,
            "node_id": "PR_node",
            "html_url": "mirror",
            "draft": True,
            "state": "open",
            "base": {"ref": "shadow/base/1", "repo": {"full_name": FORK}},
            "head": {"ref": "shadow/pr/1", "repo": {"full_name": FORK}, "sha": head},
            "body": (
                f"<!-- garnet-shadow-mirror upstream=1 upstream_head={self.source_head} "
                f"upstream_base={self.merge_base} -->"
            ),
        }

        def evidence(
            login="garnet-runtime-review[bot]",
            head_marker=head,
            summary_head=head,
            status="finalized",
            owned=True,
        ):
            summary = json.dumps({"commit": summary_head, "status": status})
            body = [
                "<!-- garnet-runtime-review -->",
                f"<!-- garnet:commit {head_marker} -->",
                f"<!-- garnet:summary {summary} -->",
            ]
            if owned:
                body.append("<!-- garnet-control-plane-pr-comment:v1:app.garnet.ai -->")
            return {"user": {"login": login}, "body": "\n".join(body)}

        malformed = {
            **evidence(),
            "body": evidence()["body"].replace('"status"', "status"),
        }
        updated = {**existing, "head": {**existing["head"], "sha": self.source_head}}
        cases = (
            ("finalized", evidence(), existing, existing, True),
            ("pending", evidence(status="pending"), existing, existing, False),
            ("untrusted", evidence(login="contributor"), existing, existing, False),
            ("no owner marker", evidence(owned=False), existing, existing, False),
            (
                "stale marker",
                evidence(head_marker=self.source_head),
                existing,
                existing,
                False,
            ),
            (
                "stale summary",
                evidence(summary_head=self.source_head),
                existing,
                existing,
                False,
            ),
            ("malformed", malformed, existing, existing, False),
            ("head moved", evidence(), existing, updated, False),
            (
                "already ready",
                evidence(),
                {**existing, "draft": False},
                existing,
                False,
            ),
        )
        self.mirror.workflow = "trusted workflow"
        fingerprint = hashlib.sha256(json.dumps([self.mirror.workflow, []]).encode()).hexdigest()
        baseline = {
            "message": f"chore: shadow baseline\n\nSource tree at {self.merge_base}; recorder {fingerprint}"
        }
        for name, comment, listed, current, promoted in cases:
            with self.subTest(name=name):
                self.api.reset_mock()
                self.api.call.side_effect = [baseline, current]
                self.api.pages.return_value = [{"body": HOLD}, comment]
                with patch.object(self.mirror, "get_ref", return_value="mirror-base"):
                    result = self.mirror.replay(
                        1, self.pr, self.files, self.merge_base, {1: listed}
                    )
                self.assertEqual(result["status"], "ready" if promoted else "unchanged")
                if promoted:
                    self.api.mark_ready.assert_called_once_with(current, 1)
                else:
                    self.api.mark_ready.assert_not_called()
                self.api.write.assert_not_called()

    def test_changed_base_creates_new_pair_without_updating_open_refs(self):
        old = {"state": "open", "number": 5, "body": "original"}
        self.mirror.workflow = "trusted workflow"
        self.api.call.return_value = {"message": "old baseline"}
        self.api.write.side_effect = [
            {"sha": "new-base"},
            {},
            {"sha": "new-head"},
            {},
            {"number": 6, "html_url": "new mirror"},
            {},
            {},
        ]
        with (
            patch.object(
                self.mirror, "get_ref", side_effect=["old-baseline", None, None]
            ),
            patch.object(self.mirror, "tree", side_effect=["base-tree", "head-tree"]),
            patch.object(self.mirror, "baseline_started", return_value=True),
        ):
            self.assertEqual(
                self.mirror.replay(1, self.pr, self.files, self.merge_base, {1: old})[
                    "status"
                ],
                "created",
            )
        writes = self.api.write.call_args_list
        self.assertEqual(
            writes[1],
            call("git/refs", {"ref": "refs/heads/shadow/base/1-r2", "sha": "new-base"}),
        )
        self.assertEqual(
            writes[3],
            call("git/refs", {"ref": "refs/heads/shadow/pr/1-r2", "sha": "new-head"}),
        )
        self.assertEqual(writes[-1], call("pulls/5", {"state": "closed"}, "PATCH"))

    def test_dry_run_never_writes(self):
        self.api.dry_run = True
        self.api.call.side_effect = [
            self.pr,
            self.pr,
            {"merge_base_commit": {"sha": self.merge_base}},
        ]
        self.api.pages.return_value = self.files
        self.assertEqual(
            self.mirror.sync(1),
            {
                "status": "dry-run",
                "upstream_base": self.merge_base,
                "upstream_head": self.source_head,
                "files": ["pnpm-lock.yaml"],
            },
        )
        self.api.write.assert_not_called()

    def test_missing_upstream_pr_does_not_abort_batch(self):
        self.api.dry_run = True
        self.api.call.side_effect = [
            APIError("GET", "pulls/1", 404, "Not Found"),
            self.pr,
            self.pr,
            {"merge_base_commit": {"sha": self.merge_base}},
        ]
        self.api.pages.side_effect = [[], self.files]
        with (
            patch("mirror.API", return_value=self.api),
            patch(
                "mirror.sys.argv",
                ["mirror", "sync", "--dry-run", "--pr", "1", "--pr", "2"],
            ),
            patch.dict("mirror.os.environ", {"SHADOW_MIRROR_TOKEN": "unused"}),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main(), 0)
        results = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [(r["pr"], r["status"]) for r in results], [(1, "skipped"), (2, "dry-run")]
        )
        self.assertEqual(results[0]["reason"], "upstream PR not found")
        self.api.write.assert_not_called()

    def test_replaced_tree_has_only_the_recorder_in_workflows(self):
        self.mirror.workflow = "trusted workflow"
        self.api.call.side_effect = [
            {"tree": {"sha": "original-root"}},
            {"tree": [{"path": ".github", "type": "tree", "sha": "original-github"}]},
        ]
        self.api.write.side_effect = [
            {"sha": "workflows"},
            {"sha": "github"},
            {"sha": "root"},
        ]
        self.assertEqual(self.mirror.tree(self.merge_base), "root")
        self.assertEqual(
            self.api.write.call_args_list,
            [
                call(
                    "git/trees",
                    {
                        "tree": [
                            {
                                "path": WORKFLOW,
                                "mode": "100644",
                                "type": "blob",
                                "content": "trusted workflow",
                            }
                        ]
                    },
                ),
                call(
                    "git/trees",
                    {
                        "tree": [
                            {
                                "path": "workflows",
                                "mode": "040000",
                                "type": "tree",
                                "sha": "workflows",
                            }
                        ],
                        "base_tree": "original-github",
                    },
                ),
                call(
                    "git/trees",
                    {
                        "base_tree": "original-root",
                        "tree": [
                            {
                                "path": ".github",
                                "mode": "040000",
                                "type": "tree",
                                "sha": "github",
                            }
                        ],
                    },
                ),
            ],
        )

    def test_recorder_runs_on_base_and_can_be_dispatched(self):
        workflow = recording_workflow(self.workflow_path.read_text())
        self.assertIn("branches: ['develop', 'record-base/**', 'shadow/base/**']", workflow)
        self.assertIn("  pull_request:\n  workflow_dispatch:\n", workflow)
        self.assertIn("    if: github.event_name != 'pull_request' || ", workflow)
        self.assertIn("  dep-install:", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 1)
        with self.assertRaises(ValueError):
            recording_workflow(workflow.replace("persist-credentials: false", ""))

    def test_replay_trees_carry_reviewer_adapters_from_develop(self):
        entry = {"mode": "100644", "type": "blob", "sha": "s"}
        self.api.call.side_effect = [
            {"commit": {"tree": {"sha": "develop-tree"}}},
            {
                "tree": [
                    {**entry, "path": "greptile.json"},
                    {**entry, "path": ".agents", "mode": "040000", "type": "tree"},
                    {**entry, "path": "REVIEW.md"},
                    {**entry, "path": "README.md"},
                    {**entry, "path": ".github", "mode": "040000", "type": "tree"},
                ]
            },
        ]
        adapters = REAL_ADAPTERS(self.mirror)
        self.assertEqual(
            [a["path"] for a in adapters], [".agents", "REVIEW.md", "greptile.json"]
        )
        self.assertEqual(
            self.api.call.call_args_list,
            [call(FORK, "commits/develop"), call(FORK, "git/trees/develop-tree")],
        )

    def test_baseline_can_be_dispatched_if_ref_creation_does_not_trigger_it(self):
        self.api.call.side_effect = [{"workflow_runs": []}] * 6 + [
            {"workflow_runs": [{"id": 1}]}
        ]
        with patch("mirror.time.sleep"):
            self.assertTrue(self.mirror.baseline_started("shadow/base/1", "base-sha"))
        self.api.write.assert_called_once_with(
            f"actions/workflows/{WORKFLOW}/dispatches", {"ref": "shadow/base/1"}
        )

    def test_gate_comment_is_only_created_when_absent(self):
        unrelated = {"id": 1, "body": "unrelated"}
        self.api.pages.return_value = [unrelated]
        self.mirror.ensure_gate_comment(3)
        self.api.pages.assert_called_once_with(FORK, "issues/3/comments")
        self.api.write.assert_called_once_with("issues/3/comments", {"body": HOLD})

        for marker in (HOLD_MARKER, GATE_MARKER):
            for verdict in ("**PASS** · Nothing new recorded", "**HOLD** · Changed"):
                with self.subTest(marker=marker, verdict=verdict):
                    self.api.reset_mock()
                    self.api.pages.return_value = [
                        unrelated,
                        {"id": 2, "body": f"{marker}\n{verdict}"},
                    ]
                    self.mirror.ensure_gate_comment(3)
                    self.api.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
