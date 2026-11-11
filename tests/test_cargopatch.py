"""Super checkout across Rust repos: rev-pin conflicts and the local [patch]."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import crux.cargopatch as cargopatch
from crux.models import BundleMember, Config

from tests.test_super import git, git_or_error, make_repo

SM = "ssh://git@github.com/sonomoshq/Service-Mesh.git"


def manifest(rev: str, extra: str = "") -> str:
    return (f'[package]\nname = "guard"\nversion = "0.1.0"\n\n[dependencies]\n'
            f'sonomos-vocab = {{ git = "{SM}", rev = "{rev}" }}\n{extra}')


class TestPinConflicts(unittest.TestCase):
    def _world(self, tmp: str, b_extra: str = "") -> tuple[str, list[BundleMember]]:
        origin = Path(tmp) / "origin.git"
        work = Path(tmp) / "seed"
        make_repo(work)
        (work / "Cargo.toml").write_text(manifest("aaa"))
        (work / "Cargo.lock").write_text('version = 3\n# aaa\n')
        git(["add", "-A"], str(work)); git(["commit", "-qm", "rust"], str(work))
        git(["init", "-q", "--bare", "-b", "main", str(origin)], tmp)
        git(["remote", "add", "origin", str(origin)], str(work))
        git(["push", "-q", "origin", "main"], str(work))
        for name, rev, extra in (("feat-a", "bbb", ""), ("feat-b", "ccc", b_extra)):
            git(["checkout", "-q", "-b", name, "main"], str(work))
            (work / "Cargo.toml").write_text(manifest(rev, extra))
            (work / "Cargo.lock").write_text(f'version = 3\n# {rev}\n')
            (work / f"{name}.txt").write_text(name)
            git(["add", "-A"], str(work)); git(["commit", "-qm", name], str(work))
            git(["push", "-q", "origin", name], str(work))
        clone = Path(tmp) / "A"
        git(["clone", "-q", str(origin), str(clone)], tmp)
        members = [BundleMember(owner="o", repo="A", branch="feat-a", pr=1),
                   BundleMember(owner="o", repo="A", branch="feat-b", pr=2)]
        return str(clone), members

    def _run(self, path, members, bundle, resolver=None):
        import crux.superact as superact
        resolver = resolver or (lambda *a, **k: self.fail("Claude should not be asked"))
        with mock.patch.object(superact, "_git", side_effect=git_or_error), \
             mock.patch("crux.llm.claude_agent_json", side_effect=resolver):
            return superact._checkout_combined(7, members, path, cfg=Config(), bundle=bundle)

    def test_a_pin_to_a_bundle_repo_is_settled_without_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path, members = self._world(tmp)
            got = self._run(path, members, {"o/a", "sonomoshq/service-mesh"})
            self.assertEqual(got[0].state, "ready", got[0].detail)
            self.assertIn("#2 only conflicted on a dependency pin", got[0].detail)
            self.assertTrue((Path(path) / "feat-b.txt").exists())
            self.assertNotIn("<<<<<<<", (Path(path) / "Cargo.toml").read_text())
            self.assertEqual(git(["status", "--porcelain", "-uno"], path), "")

    def test_a_pin_to_a_repo_outside_the_bundle_still_goes_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path, members = self._world(tmp)
            asked = []
            got = self._run(path, members, {"o/a"},
                            lambda *a, **k: asked.append(1) or {"resolved": False, "summary": "no"})
            self.assertEqual(asked, [1])
            self.assertEqual(got[0].state, "failed")

    def test_only_a_changed_pin_counts_as_pin_only(self) -> None:
        bundle = {"sonomoshq/service-mesh"}
        self.assertTrue(cargopatch._pin_only(
            f'x = {{ git = "{SM}", rev = "a" }}\n', f'x = {{ git = "{SM}", rev = "b" }}\n', bundle))
        self.assertFalse(cargopatch._pin_only(   # a feature added beside the pin
            f'x = {{ git = "{SM}", rev = "a" }}\n',
            f'x = {{ git = "{SM}", rev = "b", features = ["y"] }}\n', bundle))


class TestLocalPatch(unittest.TestCase):
    def test_apply_patches_every_crate_of_a_sibling_and_drop_takes_it_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard, sm = Path(tmp) / "Guard", Path(tmp) / "Service-Mesh"
            make_repo(guard)
            (guard / "Cargo.toml").write_text(manifest("aaa"))
            (guard / "Cargo.lock").write_text("version = 3\n")
            git(["add", "-A"], str(guard)); git(["commit", "-qm", "rust"], str(guard))
            for crate in ("vocab", "core"):
                (sm / crate).mkdir(parents=True)
                (sm / crate / "Cargo.toml").write_text(
                    f'[package]\nname = "sonomos-{crate}"\nversion = "0.16.0"\n')

            note = cargopatch.apply(str(guard), {"sonomoshq/service-mesh": str(sm)}, 26)
            self.assertIn("Service-Mesh", note)
            config = (guard / ".cargo" / "config.toml").read_text()
            self.assertIn(f'[patch."{SM}"]', config)
            self.assertIn(f'sonomos-core = {{ path = "{sm / "core"}" }}', config)
            self.assertIn(f'sonomos-vocab = {{ path = "{sm / "vocab"}" }}', config)
            self.assertEqual(git(["status", "--porcelain"], str(guard)), "")  # excluded

            (guard / "Cargo.lock").write_text("version = 3\n# rewritten by the build\n")
            self.assertTrue(cargopatch.drop(str(guard)))
            self.assertFalse((guard / ".cargo").exists())
            self.assertEqual((guard / "Cargo.lock").read_text(), "version = 3\n")

    def test_a_config_of_the_repos_own_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = Path(tmp) / "Guard"
            make_repo(guard)
            (guard / "Cargo.toml").write_text(manifest("aaa"))
            (guard / ".cargo").mkdir()
            (guard / ".cargo" / "config.toml").write_text("[alias]\ndev = 'run'\n")
            note = cargopatch.apply(str(guard), {"sonomoshq/service-mesh": tmp}, 26)
            self.assertIn("not patched", note)
            self.assertEqual((guard / ".cargo" / "config.toml").read_text(), "[alias]\ndev = 'run'\n")
            self.assertFalse(cargopatch.drop(str(guard)))


if __name__ == "__main__":
    unittest.main()
