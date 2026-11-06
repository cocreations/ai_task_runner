"""Pull before task: one pull at a time per checkout.

Each task is its own runner process and several start together; two pulls of
the same checkout in the same second collide on git's lock files and the loser
used to fail without running.

    python3 -m unittest discover -s tests -v
"""
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import runner


class PullsOfOneCheckoutDoNotOverlap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = mock.patch.object(runner, "PULL_LOCK_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def checkout(self, name):
        d = self.root / name
        (d / ".git").mkdir(parents=True)
        return str(d)

    def run_pulls(self, dirs):
        """Pull each dir on its own thread with git stubbed; return the most
        git commands seen running at once, per checkout and overall."""
        guard = threading.Lock()
        running = {}
        peak = {}
        peak_all = [0]

        def fake_git(cmd, **kwargs):
            where = cmd[2]
            if cmd[3] == "pull":
                with guard:
                    running[where] = running.get(where, 0) + 1
                    peak[where] = max(peak.get(where, 0), running[where])
                    peak_all[0] = max(peak_all[0], sum(running.values()))
                time.sleep(0.15)
                with guard:
                    running[where] -= 1
            return mock.Mock(returncode=0, stdout="abc1234 subject\n" if cmd[3] == "log" else "", stderr="")

        results = []
        with mock.patch.object(runner.subprocess, "run", fake_git):
            threads = [threading.Thread(target=lambda d=d: results.append(runner.pull_project(d))) for d in dirs]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        return results, peak, peak_all[0]

    def test_same_checkout_pulls_run_one_at_a_time(self):
        repo = self.checkout("repo")
        results, peak, _ = self.run_pulls([repo] * 4)
        self.assertEqual(results, [(True, "now at abc1234 subject")] * 4)
        self.assertEqual(peak[repo], 1)

    def test_different_checkouts_do_not_wait_for_each_other(self):
        a, b = self.checkout("a"), self.checkout("b")
        results, peak, peak_all = self.run_pulls([a, b])
        self.assertEqual(len(results), 2)
        self.assertEqual((peak[a], peak[b]), (1, 1))
        self.assertEqual(peak_all, 2)

    def test_the_lock_is_released_after_a_failed_pull(self):
        repo = self.checkout("repo")

        def failing(cmd, **kwargs):
            bad = cmd[3] == "pull"
            return mock.Mock(returncode=1 if bad else 0, stdout="", stderr="fatal: no route\n" if bad else "")

        with mock.patch.object(runner.subprocess, "run", failing):
            self.assertEqual(runner.pull_project(repo), (False, "git pull --ff-only failed: fatal: no route"))
        results, peak, _ = self.run_pulls([repo])
        self.assertEqual(results, [(True, "now at abc1234 subject")])

    def test_the_lock_file_lives_outside_the_checkout(self):
        repo = self.checkout("repo")
        self.run_pulls([repo])
        self.assertEqual([p.name for p in Path(repo).iterdir()], [".git"])
        self.assertEqual(list((Path(repo) / ".git").iterdir()), [])
        self.assertEqual(len(list(self.root.glob("atr-pull-*.lock"))), 1)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RealGitConcurrentPulls(unittest.TestCase):
    """Six pulls of one real clone at once, with new commits to fetch: all succeed."""

    def test_all_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                   "HOME": tmp, "PATH": __import__("os").environ["PATH"]}

            def git(*args, cwd):
                subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

            origin = root / "origin"
            origin.mkdir()
            git("init", "-q", "-b", "main", cwd=origin)
            (origin / "a.txt").write_text("one\n")
            git("add", ".", cwd=origin)
            git("commit", "-q", "-m", "one", cwd=origin)
            git("clone", "-q", str(origin), str(root / "clone"), cwd=root)
            for i in range(20):
                (origin / f"f{i}.txt").write_text("x" * 2000)
            git("add", ".", cwd=origin)
            git("commit", "-q", "-m", "two", cwd=origin)

            results = []
            with mock.patch.object(runner, "PULL_LOCK_DIR", root), \
                    mock.patch.dict("os.environ", env):
                threads = [threading.Thread(target=lambda: results.append(runner.pull_project(str(root / "clone"))))
                           for _ in range(6)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(60)
            self.assertEqual(len(results), 6)
            for ok, msg in results:
                self.assertTrue(ok, msg)
                self.assertTrue(msg.endswith(" two"), msg)


if __name__ == "__main__":
    unittest.main()
