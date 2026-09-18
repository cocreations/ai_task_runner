"""One Claude session runs one task at a time; a parked task is still a task;
a task is only readable by its submitter or its project's users.

    python3 -m unittest discover -s tests -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import frontmatter

import runner
import server


def write_task(folder: Path, task_id: str, **meta) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post("the prompt", id=task_id, user="alice", project="ai-mode",
                            status=folder.name, created_at="2026-09-18T04:48:54+00:00", **meta)
    path = folder / f"{task_id}.md"
    path.write_text(frontmatter.dumps(post))
    return path


class RunnerSessionSerialisation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.queued, self.processing = root / "queued", root / "processing"
        self.queued.mkdir()
        self.processing.mkdir()
        patches = [mock.patch.object(runner, "QUEUED_DIR", self.queued),
                   mock.patch.object(runner, "PROCESSING_DIR", self.processing),
                   mock.patch.object(runner, "log", lambda *_: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def claim(self):
        return runner.claim_next_task(runner.get_queued_tasks())

    def test_a_task_waits_while_its_session_is_running(self):
        write_task(self.processing, "20260918_044854_aaaa", claude_session_id="S1")
        write_task(self.queued, "20260918_044934_bbbb", claude_session_id="S1")
        self.assertIsNone(self.claim())
        self.assertTrue((self.queued / "20260918_044934_bbbb.md").exists(), "it keeps its place in the queue")

    def test_the_next_eligible_task_is_taken_instead(self):
        write_task(self.processing, "20260918_044854_aaaa", claude_session_id="S1")
        write_task(self.queued, "20260918_044934_bbbb", claude_session_id="S1")
        write_task(self.queued, "20260918_045000_cccc", claude_session_id="S2")
        claimed = self.claim()
        self.assertEqual(claimed.name, "20260918_045000_cccc.md")

    def test_the_waiting_task_runs_once_the_session_is_free(self):
        running = write_task(self.processing, "20260918_044854_aaaa", claude_session_id="S1")
        write_task(self.queued, "20260918_044934_bbbb", claude_session_id="S1")
        self.assertIsNone(self.claim())
        running.unlink()
        self.assertEqual(self.claim().name, "20260918_044934_bbbb.md")

    def test_tasks_without_a_session_are_not_held_back(self):
        write_task(self.processing, "20260918_044854_aaaa")
        write_task(self.queued, "20260918_044934_bbbb")
        self.assertEqual(self.claim().name, "20260918_044934_bbbb.md")

    def test_two_runners_claiming_at_once_the_younger_gives_way(self):
        # Another runner claims the OLDER task of the same session in the instant
        # between this runner's look at processing/ and its own claim.
        write_task(self.queued, "20260918_044934_bbbb", claude_session_id="S1")
        real_claim = runner.claim_task

        def claim_with_a_rival(task_path):
            claimed = real_claim(task_path)
            write_task(self.processing, "20260918_044854_aaaa", claude_session_id="S1")
            return claimed

        with mock.patch.object(runner, "claim_task", claim_with_a_rival):
            self.assertIsNone(self.claim())
        self.assertTrue((self.queued / "20260918_044934_bbbb.md").exists(), "back in the queue")
        self.assertFalse((self.processing / "20260918_044934_bbbb.md").exists())

    def test_the_older_of_two_simultaneous_claims_keeps_it(self):
        write_task(self.queued, "20260918_044854_aaaa", claude_session_id="S1")
        real_claim = runner.claim_task

        def claim_with_a_rival(task_path):
            claimed = real_claim(task_path)
            write_task(self.processing, "20260918_044934_bbbb", claude_session_id="S1")
            return claimed

        with mock.patch.object(runner, "claim_task", claim_with_a_rival):
            self.assertEqual(self.claim().name, "20260918_044854_aaaa.md")


class ServerTaskLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dirs = {name: root / "tasks" / name for name in server.STATUS_DIRS}
        self.logs = root / "logs"
        self.logs.mkdir()
        self.user = "alice"
        patches = [mock.patch.object(server, "STATUS_DIRS", self.dirs),
                   mock.patch.object(server, "LOGS_DIR", self.logs),
                   mock.patch.object(server, "get_current_user", lambda: self.user),
                   mock.patch.object(server, "_projects", {"ai-mode": {"allowed_users": ["alice", "carol"]}})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_a_parked_task_is_found_with_its_reset_time(self):
        write_task(self.dirs["awaiting_credits"], "t1", credit_reset_at="2026-09-18T06:00:00+00:00")
        out = server.task_status("t1")
        self.assertIn("Status: awaiting_credits", out)
        self.assertIn("Credit reset: 2026-09-18T06:00:00+00:00", out)
        self.assertIn("still 'awaiting_credits'", server.task_output("t1"))

    def test_a_queued_task_reports_its_place(self):
        write_task(self.dirs["queued"], "20260918_044854_aaaa")
        write_task(self.dirs["queued"], "20260918_044934_bbbb")
        self.assertIn("Queue position: 2", server.task_status("20260918_044934_bbbb"))
        self.assertIn("Queue position: 1", server.task_status("20260918_044854_aaaa"))

    def test_the_header_comes_before_the_prompt(self):
        write_task(self.dirs["queued"], "t1")
        header, _, prompt = server.task_status("t1").partition("--- Prompt ---")
        self.assertIn("Queue position", header)
        self.assertIn("the prompt", prompt)

    def test_a_parked_task_can_be_cancelled(self):
        write_task(self.dirs["awaiting_credits"], "t1", credit_reset_at="2026-09-18T06:00:00+00:00")
        self.dirs["failed"].mkdir(parents=True)
        self.assertIn("cancelled", server.cancel_task("t1"))
        self.assertFalse((self.dirs["awaiting_credits"] / "t1.md").exists())
        self.assertIn("Status: cancelled", server.task_status("t1"))

    def test_a_running_task_cannot_be_cancelled(self):
        write_task(self.dirs["processing"], "t1")
        self.assertIn("Cannot cancel", server.cancel_task("t1"))

    def test_strangers_cannot_read_a_task(self):
        write_task(self.dirs["done"], "t1")
        (self.logs / "t1.log").write_text("secret run log")
        self.user = "mallory"  # not the submitter, not on the project
        self.assertEqual(server.task_status("t1"), "Task 't1' not found.")
        self.assertEqual(server.task_output("t1"), "Task 't1' not found.")
        self.assertEqual(server.cancel_task("t1"), "Task 't1' not found.")
        self.assertIn("No tasks found", server.list_tasks())

    def test_a_project_colleague_can(self):
        write_task(self.dirs["done"], "t1")
        (self.logs / "t1.log").write_text("the run log")
        self.user = "carol"
        self.assertIn("Status: done", server.task_status("t1"))
        self.assertEqual(server.task_output("t1"), "the run log")


if __name__ == "__main__":
    unittest.main()
