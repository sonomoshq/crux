# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.ghrest and post.py's gh-less REST fallback.

All network use is mocked (urllib.request.urlopen); the fallback wiring in
crux.post is exercised by making subprocess.run raise FileNotFoundError, which
is exactly what a host without `gh` (a claude.ai/code cloud container) does.
"""
from __future__ import annotations

import dataclasses
import io
import json
import unittest
import urllib.error
from unittest import mock

from crux import ghrest, post
from crux.models import PostError, RepoInfo


def make_info(branch: str = "feat/buffering") -> RepoInfo:
    return RepoInfo(
        root="/repo",
        branch=branch,
        head_sha="h" * 40,
        base_sha="b" * 40,
        owner="example-org",
        repo="crux",
    )


class FakeResponse:
    """Duck-types the urlopen context manager: read() + headers."""

    def __init__(self, body: str, link: str = ""):
        self._body = body
        self.headers = {"Link": link} if link else {}

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _no_creds():
    return mock.patch("crux.ghrest.load_credentials", return_value={})


# ---------------------------------------------------------------------------
# ghrest.github_token
# ---------------------------------------------------------------------------

class TestGithubToken(unittest.TestCase):
    def test_gh_token_env_wins(self) -> None:
        env = {"GH_TOKEN": "gh-tok", "GITHUB_TOKEN": "github-tok"}
        with mock.patch.dict("os.environ", env, clear=False), _no_creds():
            self.assertEqual(ghrest.github_token(), "gh-tok")

    def test_github_token_env_is_second(self) -> None:
        env = {"GH_TOKEN": "", "GITHUB_TOKEN": "github-tok"}
        with mock.patch.dict("os.environ", env, clear=False), _no_creds():
            self.assertEqual(ghrest.github_token(), "github-tok")

    def test_credentials_file_is_last(self) -> None:
        env = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch("crux.ghrest.load_credentials",
                        return_value={"github_token": " file-tok "}):
            self.assertEqual(ghrest.github_token(), "file-tok")

    def test_no_source_is_empty(self) -> None:
        env = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}
        with mock.patch.dict("os.environ", env, clear=False), _no_creds():
            self.assertEqual(ghrest.github_token(), "")

    def test_non_string_credentials_value_is_empty(self) -> None:
        env = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch("crux.ghrest.load_credentials",
                        return_value={"github_token": 42}):
            self.assertEqual(ghrest.github_token(), "")


# ---------------------------------------------------------------------------
# ghrest.rest_call
# ---------------------------------------------------------------------------

def _token():
    return mock.patch("crux.ghrest.github_token", return_value="tok")


class TestRestCall(unittest.TestCase):
    def test_no_token_raises_with_guidance(self) -> None:
        with mock.patch("crux.ghrest.github_token", return_value=""):
            with self.assertRaises(PostError) as ctx:
                ghrest.rest_call("GET", "repos/o/r/pulls/1")
        message = str(ctx.exception)
        self.assertIn("GH_TOKEN", message)
        self.assertIn("credentials.json", message)

    def test_no_token_hint_does_not_send_cloud_users_after_a_token(self) -> None:
        """A claude.ai/code session cannot post with ANY token — its egress
        proxy ignores the one supplied and blocks raw writes to
        api.github.com (README, "Posting goes through the session's GitHub
        tools"). So the error a user reads there must not send them off to
        create an environment secret, an errand we already know fails; it has
        to name the route that works. Token advice belongs to generic gh-less
        hosts."""
        for text in (ghrest.NO_TOKEN_HINT, ghrest.__doc__ or ""):
            self.assertNotIn("environment secret", text)
            self.assertIn("claude.ai/code", text)
            self.assertIn("MCP", text)
            self.assertIn("crux preview", text)

    def test_get_returns_body_and_sends_auth(self) -> None:
        with _token(), mock.patch("urllib.request.urlopen",
                                  return_value=FakeResponse('{"number": 7}')) as opened:
            out = ghrest.rest_call("GET", "repos/o/r/pulls/7")
        self.assertEqual(json.loads(out), {"number": 7})
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/o/r/pulls/7")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertIsNone(request.data)

    def test_post_sends_json_body(self) -> None:
        payload = json.dumps({"body": "hi"})
        with _token(), mock.patch("urllib.request.urlopen",
                                  return_value=FakeResponse("{}")) as opened:
            ghrest.rest_call("POST", "repos/o/r/issues/1/comments", payload)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, payload.encode("utf-8"))
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_paginate_follows_next_and_concatenates(self) -> None:
        page2_url = "https://api.github.com/repositories/1/issues/5/comments?page=2"
        responses = [
            FakeResponse('[{"id": 1}]', link=f'<{page2_url}>; rel="next"'),
            FakeResponse('[{"id": 2}]'),
        ]
        with _token(), mock.patch("urllib.request.urlopen",
                                  side_effect=responses) as opened:
            out = ghrest.rest_call("GET", "repos/o/r/issues/5/comments",
                                   paginate=True)
        # Both pages fetched; the second at the Link URL verbatim.
        first_url = opened.call_args_list[0].args[0].full_url
        self.assertIn("per_page=100", first_url)
        self.assertEqual(opened.call_args_list[1].args[0].full_url, page2_url)
        # Concatenated pages parse exactly like `gh api --paginate` output.
        ids = [c["id"] for c in post._iter_paginated(out)]
        self.assertEqual(ids, [1, 2])

    def test_non_paginate_ignores_link_header(self) -> None:
        response = FakeResponse("[]", link='<https://x>; rel="next"')
        with _token(), mock.patch("urllib.request.urlopen",
                                  return_value=response) as opened:
            ghrest.rest_call("GET", "repos/o/r/pulls")
        self.assertEqual(opened.call_count, 1)

    def test_http_error_raises_posterror_with_code(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.github.com/x", 422, "Unprocessable", None,
            io.BytesIO(b'{"message": "Validation Failed"}'))
        with _token(), mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(PostError) as ctx:
                ghrest.rest_call("POST", "repos/o/r/pulls", "{}")
        message = str(ctx.exception)
        self.assertIn("HTTP 422", message)
        self.assertIn("Validation Failed", message)

    def test_network_error_raises_posterror(self) -> None:
        with _token(), mock.patch("urllib.request.urlopen",
                                  side_effect=urllib.error.URLError("down")):
            with self.assertRaises(PostError):
                ghrest.rest_call("GET", "repos/o/r/pulls")


class TestNextLink(unittest.TestCase):
    def test_parses_next_among_rels(self) -> None:
        header = ('<https://api.github.com/x?page=2>; rel="next", '
                  '<https://api.github.com/x?page=9>; rel="last"')
        self.assertEqual(ghrest._next_link(header),
                         "https://api.github.com/x?page=2")

    def test_no_next_is_none(self) -> None:
        self.assertIsNone(ghrest._next_link('<https://x?page=1>; rel="prev"'))
        self.assertIsNone(ghrest._next_link(""))


# ---------------------------------------------------------------------------
# post._run_gh fallback wiring (subprocess raises FileNotFoundError, i.e. a
# host with no gh binary)
# ---------------------------------------------------------------------------

def _no_gh():
    return mock.patch("crux.post.subprocess.run", side_effect=FileNotFoundError)


class TestRunGhFallback(unittest.TestCase):
    def test_api_argv_translates_mechanically(self) -> None:
        with _no_gh(), mock.patch("crux.ghrest.rest_call",
                                  return_value="[]") as rest:
            post._run_gh(["api", "repos/o/r/issues/5/comments", "--paginate"],
                         cwd="/repo")
        rest.assert_called_once_with("GET", "repos/o/r/issues/5/comments",
                                     None, paginate=True, timeout=mock.ANY)

    def test_api_argv_with_method_and_body(self) -> None:
        payload = json.dumps({"body": "card"})
        with _no_gh(), mock.patch("crux.ghrest.rest_call",
                                  return_value="{}") as rest:
            post._run_gh(
                ["api", "-X", "PATCH", "repos/o/r/issues/comments/9",
                 "--input", "-"],
                stdin_text=payload)
        rest.assert_called_once_with("PATCH", "repos/o/r/issues/comments/9",
                                     payload, paginate=False, timeout=mock.ANY)

    def test_explicit_rest_spec_with_payload(self) -> None:
        with _no_gh(), mock.patch("crux.ghrest.rest_call",
                                  return_value="{}") as rest:
            post._run_gh(["pr", "create", "--base", "main"],
                         rest=("POST", "repos/o/r/pulls", {"base": "main"}))
        rest.assert_called_once_with("POST", "repos/o/r/pulls",
                                     json.dumps({"base": "main"}),
                                     timeout=mock.ANY)

    # --- `gh api --jq`: one field must stay one field ---------------------
    # main grew several `--jq` call sites while this branch was open (pr_base,
    # the super-PR author/admin gates). Handing those the whole JSON document
    # instead of the field they asked for is silently wrong in the worst
    # place — an author gate that matches nobody — so the fallback evaluates
    # the dotted path itself.

    def test_jq_field_path_is_applied(self) -> None:
        with _no_gh(), mock.patch(
                "crux.ghrest.rest_call",
                return_value=json.dumps({"base": {"ref": "release/2"}})):
            out = post._run_gh(["api", "repos/o/r/pulls/7", "--jq", ".base.ref"])
        self.assertEqual(out, "release/2")

    def test_jq_non_string_scalar_prints_as_json(self) -> None:
        with _no_gh(), mock.patch(
                "crux.ghrest.rest_call",
                return_value=json.dumps({"permissions": {"admin": True}})):
            out = post._run_gh(["api", "repos/o/r", "--jq",
                                ".permissions.admin"])
        self.assertEqual(out, "true")

    def test_jq_unresolvable_path_is_empty(self) -> None:
        with _no_gh(), mock.patch("crux.ghrest.rest_call",
                                  return_value=json.dumps({"user": None})):
            self.assertEqual(
                post._run_gh(["api", "repos/o/r/pulls/7", "--jq",
                              ".user.login"]), "")

    def test_jq_beyond_a_field_path_is_refused_not_guessed(self) -> None:
        with _no_gh(), mock.patch("crux.ghrest.rest_call",
                                  return_value="[]"):
            with self.assertRaises(PostError) as ctx:
                post._run_gh(["api", "repos/o/r/pulls", "--jq",
                              ".[] | .number"])
        self.assertIn("not a plain field path", str(ctx.exception))

    def test_pr_base_via_rest_returns_the_branch(self) -> None:
        """The D34 base lookup on a gh-less host: a branch name, not the PR."""
        info = make_info()
        with _no_gh(), mock.patch(
                "crux.ghrest.rest_call",
                return_value=json.dumps({"number": 7,
                                         "base": {"ref": "release/2"}})):
            self.assertEqual(post.pr_base(info, 7), "release/2")

    def test_find_pr_via_rest_reads_the_rest_base_shape(self) -> None:
        """Stacked PRs, gh-less host: REST rows carry base.ref where gh's
        --json carries baseRefName, and _choose_pr must pick the same PR
        either way (here: the one based on the branch being reviewed)."""
        info = dataclasses.replace(make_info(), crux_base="parent")
        rows = json.dumps([
            {"number": 11, "base": {"ref": "main"}},
            {"number": 12, "base": {"ref": "parent"}},
        ])
        with _no_gh(), mock.patch("crux.ghrest.rest_call", return_value=rows):
            self.assertEqual(post.find_pr(info), 12)

    def test_pr_meta_via_rest_reads_the_pr_object(self) -> None:
        """The Slack line's (created_at, author) lookup on a gh-less host.

        `gh pr view` argv names no endpoint, so with no explicit REST spec the
        fallback raised "gh not installed" and pr_meta — best-effort by
        design — swallowed it into ("", ""): the announcement silently lost
        its date and its credit. REST spells the same two facts `created_at`
        and `user`, and has no `user.name`, so the login is the name here.
        """
        info = make_info()
        with _no_gh(), mock.patch(
                "crux.ghrest.rest_call",
                return_value=json.dumps({
                    "created_at": "2026-07-23T02:05:25Z",
                    "user": {"login": "fixture-alpha"},
                })) as rest:
            self.assertEqual(post.pr_meta(info, 13),
                             ("2026-07-23T02:05:25Z", "fixture-alpha"))
        rest.assert_called_once_with("GET", "repos/example-org/crux/pulls/13",
                                     None, timeout=mock.ANY)

    def test_non_api_without_spec_keeps_old_error(self) -> None:
        with _no_gh():
            with self.assertRaises(PostError) as ctx:
                post._run_gh(["auth", "status"])
        self.assertEqual(str(ctx.exception), "gh not installed")

    def test_find_pr_via_rest(self) -> None:
        info = make_info()
        with _no_gh(), mock.patch("crux.ghrest.rest_call",
                                  return_value='[{"number": 42, "state": "open"}]') as rest:
            self.assertEqual(post.find_pr(info), 42)
        method, path = rest.call_args.args[:2]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "repos/example-org/crux/pulls"
                               "?head=example-org%3Afeat%2Fbuffering&state=open")

    def test_find_pr_via_rest_none_when_empty(self) -> None:
        info = make_info()
        with _no_gh(), mock.patch("crux.ghrest.rest_call", return_value="[]"):
            self.assertIsNone(post.find_pr(info))

    def test_upsert_comment_via_rest_posts_when_no_marker(self) -> None:
        info = make_info()
        calls = []

        def fake_rest(method, path, body=None, paginate=False, timeout=0):
            calls.append((method, path, body))
            return "[]" if method == "GET" else "{}"

        with _no_gh(), mock.patch("crux.ghrest.rest_call", side_effect=fake_rest):
            post.upsert_comment(info, 5, "<!-- m -->\nbody", marker="<!-- m -->")
        self.assertEqual(calls[0][:2],
                         ("GET", "repos/example-org/crux/issues/5/comments"))
        method, path, body = calls[1]
        self.assertEqual((method, path),
                         ("POST", "repos/example-org/crux/issues/5/comments"))
        self.assertEqual(json.loads(body), {"body": "<!-- m -->\nbody"})

    def test_no_token_surfaces_actionable_posterror(self) -> None:
        info = make_info()
        env = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}
        with _no_gh(), mock.patch.dict("os.environ", env, clear=False), \
             mock.patch("crux.ghrest.load_credentials", return_value={}):
            with self.assertRaises(PostError) as ctx:
                post.find_pr(info)
        self.assertIn("GH_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
