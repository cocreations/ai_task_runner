"""A credit hold is announced once, its lift once, over the webhooks in
notifications.yaml; a queued task behind a hold says so; and none of it can
break the runner.

    python3 -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import frontmatter

import credit_hold
import notify
import runner
import server

FUTURE = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def write_task(folder: Path, task_id: str, **meta) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    fields = {"user": "alice", "project": "ai-mode", "status": folder.name,
              "created_at": "2026-11-05T04:00:00+00:00", **meta}
    post = frontmatter.Post("the prompt", id=task_id, **fields)
    path = folder / f"{task_id}.md"
    path.write_text(frontmatter.dumps(post))
    return path


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


def posted_bodies(urlopen_mock):
    return [json.loads(call.args[0].data.decode()) for call in urlopen_mock.call_args_list]


class LoadWebhooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text):
        (self.dir / notify.CONFIG_NAME).write_text(text)

    def test_absent_file_means_no_webhooks(self):
        self.assertEqual(notify.load_webhooks(self.dir), [])

    def test_entries_with_events(self):
        self.write("webhooks:\n  - url: https://discord.com/api/webhooks/1/abc\n"
                   "    events: [credit_limit]\n  - https://example.com/hook\n")
        self.assertEqual(notify.load_webhooks(self.dir), [
            {"url": "https://discord.com/api/webhooks/1/abc", "events": ["credit_limit"]},
            {"url": "https://example.com/hook", "events": ["credit_limit", "credit_restored"]},
        ])

    def test_only_https_and_only_well_formed(self):
        self.write("webhooks:\n  - url: http://plain.example/hook\n  - 42\n  - url: https://ok.example/h\n    events: nope\n")
        self.assertEqual(notify.load_webhooks(self.dir), [])

    def test_garbage_yaml_means_no_webhooks_and_no_exception(self):
        self.write("webhooks: [unclosed")
        with mock.patch.object(notify, "log"):
            self.assertEqual(notify.load_webhooks(self.dir), [])


class Content(unittest.TestCase):
    def test_account_is_the_first_host_label(self):
        self.assertEqual(notify.account_name("https://moment.ai-task-runner.com"), "moment")
        self.assertEqual(notify.account_name("https://my-own-event-app.ai-task-runner.com/"), "my-own-event-app")
        self.assertEqual(notify.account_name(""), "this runner")

    def test_when_is_absolute_and_relative(self):
        now = datetime(2026, 11, 4, 14, 50, tzinfo=timezone.utc)
        self.assertEqual(notify.when("2026-11-04T17:00:00+00:00", now), "2026-11-04 17:00 UTC (in 2h 10m)")
        self.assertEqual(notify.when("2026-11-04T15:05:00+00:00", now), "2026-11-04 15:05 UTC (in 15m)")
        self.assertEqual(notify.when("2026-11-04T14:00:00+00:00", now), "2026-11-04 14:00 UTC (now)")
        self.assertEqual(notify.when("not a time", now), "not a time")

    def test_credit_limit_line_names_account_kind_count_and_task(self):
        now = datetime(2026, 11, 4, 14, 50, tzinfo=timezone.utc)
        text = notify.build_content("credit_limit", {
            "account": "moment", "limit_kind": "session", "parked_tasks": 4,
            "reset_at": "2026-11-04T17:00:00+00:00",
            "project": "moment-marketing-pipeline-agents", "task_id": "20261104_1430_ab12"}, now)
        self.assertEqual(text, "⚠️ Task Runner **moment** hit its Claude session limit — 4 tasks parked, "
                               "resumes 2026-11-04 17:00 UTC (in 2h 10m). "
                               "Last task: moment-marketing-pipeline-agents / 20261104_1430_ab12.")

    def test_restored_line(self):
        text = notify.build_content("credit_restored", {"account": "moment", "limit_kind": "session"})
        self.assertEqual(text, "✅ Task Runner **moment** is running again — the Claude session limit has lifted.")

    def test_payload_is_discord_shaped_with_machine_fields(self):
        with mock.patch.dict("os.environ", {"SERVER_URL": "https://moment.ai-task-runner.com"}):
            p = notify.build_payload("credit_limit", {"limit_kind": "weekly", "reset_at": FUTURE,
                                                      "parked_tasks": 2, "project": "p", "task_id": "t"})
        self.assertEqual(p["username"], "AI Task Runner")
        self.assertIn("**moment**", p["content"])
        self.assertEqual(p["event"], "credit_limit")
        self.assertEqual(p["account"], "moment")
        self.assertEqual((p["limit_kind"], p["parked_tasks"], p["project"], p["task_id"]),
                         ("weekly", 2, "p", "t"))
        names = [f["name"] for f in p["embeds"][0]["fields"]]
        self.assertEqual(names, ["account", "limit kind", "reset at", "parked tasks", "project", "task id"])
        self.assertEqual(set(notify.discord_only(p)), {"content", "username", "embeds"})


class Send(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / notify.CONFIG_NAME).write_text(
            "webhooks:\n  - url: https://discord.com/api/webhooks/1/abc\n    events: [credit_limit]\n")
        p = mock.patch.object(notify, "log")
        p.start()
        self.addCleanup(p.stop)

    def send(self, event="credit_limit", **fields):
        return notify.send(event, config_dir=self.dir, limit_kind="session", reset_at=FUTURE,
                           parked_tasks=1, **fields)

    def test_no_file_no_post(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(notify.send("credit_limit", config_dir=self.dir / "nowhere"), 0)
        urlopen.assert_not_called()

    def test_unsubscribed_event_is_not_sent(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(self.send("credit_restored"), 0)
        urlopen.assert_not_called()

    def test_one_post_with_the_full_body(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(204)) as urlopen:
            self.assertEqual(self.send(project="p", task_id="t"), 1)
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://discord.com/api/webhooks/1/abc")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = posted_bodies(urlopen)[0]
        self.assertEqual(body["event"], "credit_limit")
        self.assertIn("content", body)

    def test_transport_error_then_success(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[urllib.error.URLError("down"), FakeResponse(204)]) as urlopen:
            self.assertEqual(self.send(), 1)
        self.assertEqual(urlopen.call_count, 2)

    def test_persistent_transport_error_is_swallowed(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")) as urlopen:
            self.assertEqual(self.send(), 0)
        self.assertEqual(urlopen.call_count, notify.MAX_ATTEMPTS)

    def test_http_400_falls_back_to_the_discord_only_body(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[http_error(400), FakeResponse(204)]) as urlopen:
            self.assertEqual(self.send(), 1)
        first, second = posted_bodies(urlopen)
        self.assertIn("event", first)
        self.assertEqual(set(second), {"content", "username", "embeds"})

    def test_other_4xx_is_not_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(404)) as urlopen:
            self.assertEqual(self.send(), 0)
        self.assertEqual(urlopen.call_count, 1)


class RunnerParksAndAnnouncesOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.processing = self.tasks / "processing"
        self.credits = self.tasks / "awaiting_credits"
        self.weekly = self.tasks / "awaiting_weekly_reset"
        for d in (self.processing, self.credits, self.weekly):
            d.mkdir(parents=True)
        patches = [mock.patch.object(runner, "TASKS_DIR", self.tasks),
                   mock.patch.object(runner, "PROCESSING_DIR", self.processing),
                   mock.patch.object(runner, "AWAITING_CREDITS_DIR", self.credits),
                   mock.patch.object(runner, "AWAITING_WEEKLY_RESET_DIR", self.weekly),
                   mock.patch.object(runner, "log", lambda *_: None),
                   mock.patch.object(runner.notify, "send")]
        for p in patches:
            started = p.start()
            self.addCleanup(p.stop)
        self.send = runner.notify.send
        self.reset_at = datetime.now(timezone.utc) + timedelta(hours=2)

    def park(self, task_id, kind="session"):
        path = write_task(self.processing, task_id, project="moment-marketing-pipeline-agents")
        runner.finalize_awaiting(path, kind, self.reset_at, self.reset_at.isoformat(), 1)

    def test_first_park_notifies_and_marks(self):
        self.park("20261105_040000_aaaa")
        self.send.assert_called_once()
        event, kwargs = self.send.call_args.args[0], self.send.call_args.kwargs
        self.assertEqual(event, "credit_limit")
        self.assertEqual(kwargs["limit_kind"], "session")
        self.assertEqual(kwargs["parked_tasks"], 1)
        self.assertEqual(kwargs["project"], "moment-marketing-pipeline-agents")
        self.assertEqual(kwargs["task_id"], "20261105_040000_aaaa")
        self.assertEqual(credit_hold.read_marker(self.tasks)["kind"], "session")
        self.assertTrue((self.credits / "20261105_040000_aaaa.md").exists())

    def test_second_park_on_the_same_hold_stays_quiet(self):
        self.park("20261105_040000_aaaa")
        self.park("20261105_040001_bbbb")
        self.send.assert_called_once()
        self.assertEqual(len(list(self.credits.glob("*.md"))), 2)

    def test_weekly_limit_parks_in_the_weekly_dir(self):
        self.park("20261105_040000_aaaa", kind="weekly")
        self.assertTrue((self.weekly / "20261105_040000_aaaa.md").exists())
        self.assertEqual(self.send.call_args.kwargs["limit_kind"], "weekly")

    def test_hold_lift_is_announced_once_and_forgotten(self):
        self.park("20261105_040000_aaaa")
        self.send.reset_mock()
        runner.maybe_notify_restored()          # still parked with a future reset
        self.send.assert_not_called()
        (self.credits / "20261105_040000_aaaa.md").unlink()   # re-queued / cancelled
        runner.maybe_notify_restored()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], "credit_restored")
        self.assertEqual(self.send.call_args.kwargs["limit_kind"], "session")
        self.assertIsNone(credit_hold.read_marker(self.tasks))
        runner.maybe_notify_restored()          # nothing recorded now
        self.send.assert_called_once()

    def test_a_new_hold_after_a_lift_is_announced_again(self):
        self.park("20261105_040000_aaaa")
        (self.credits / "20261105_040000_aaaa.md").unlink()
        runner.maybe_notify_restored()
        self.park("20261105_050000_cccc")
        self.assertEqual([c.args[0] for c in self.send.call_args_list],
                         ["credit_limit", "credit_restored", "credit_limit"])

    def test_a_failing_webhook_never_breaks_parking(self):
        self.send.side_effect = RuntimeError("boom")
        self.park("20261105_040000_aaaa")
        self.assertTrue((self.credits / "20261105_040000_aaaa.md").exists())

    def test_structured_rate_limit_event_end_to_end(self):
        log = self.tasks / "t.log"
        log.write_text('{"type":"assistant","message":{}}\n'
                       '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
                       '"resetsAt":4102444800,"rateLimitType":"five_hour"}}\n')
        hit, kind, reset_raw, reset_dt = runner.detect_credit_limit(log)
        self.assertTrue(hit)
        self.assertEqual(kind, "five_hour")
        self.assertIsNone(reset_raw)
        self.assertEqual(reset_dt, datetime(2100, 1, 1, tzinfo=timezone.utc))
        path = write_task(self.processing, "20261105_040000_aaaa", project="p")
        runner.finalize_awaiting(path, kind, reset_dt, reset_dt.isoformat(), 1)
        parked = frontmatter.load(str(self.credits / "20261105_040000_aaaa.md")).metadata
        self.assertEqual(parked["status"], "awaiting_credits")
        self.assertEqual(parked["credit_limit_kind"], "five_hour")
        self.assertEqual(parked["credit_reset_at"], reset_dt.isoformat())
        self.assertEqual(runner.earliest_pending_credit_hold(), reset_dt)


class ServerTellsQueuedTasksAboutTheHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dirs = {name: root / "tasks" / name for name in server.STATUS_DIRS}
        patches = [mock.patch.object(server, "STATUS_DIRS", self.dirs),
                   mock.patch.object(server, "get_current_user", lambda: "alice"),
                   mock.patch.object(server, "_projects", {"ai-mode": {"allowed_users": ["alice"]}})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_queued_task_behind_a_hold_says_so(self):
        write_task(self.dirs["queued"], "t1")
        write_task(self.dirs["awaiting_credits"], "t0", credit_reset_at=FUTURE, credit_limit_kind="session")
        header = server.task_status("t1").partition("--- Prompt ---")[0]
        self.assertIn(f"Credit hold: until {FUTURE} (session)", header)
        self.assertIn("Queue position: 1", header)

    def test_the_earliest_of_several_holds_wins(self):
        later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        write_task(self.dirs["queued"], "t1")
        write_task(self.dirs["awaiting_weekly_reset"], "t0", credit_reset_at=later, credit_limit_kind="weekly")
        write_task(self.dirs["awaiting_credits"], "t2", credit_reset_at=FUTURE, credit_limit_kind="session")
        self.assertIn(f"Credit hold: until {FUTURE} (session)", server.task_status("t1"))

    def test_a_hold_whose_reset_has_passed_is_not_a_hold(self):
        write_task(self.dirs["queued"], "t1")
        write_task(self.dirs["awaiting_credits"], "t0", credit_reset_at=PAST)
        self.assertNotIn("Credit hold", server.task_status("t1"))

    def test_no_parked_task_no_line(self):
        write_task(self.dirs["queued"], "t1")
        self.assertNotIn("Credit hold", server.task_status("t1"))

    def test_running_and_parked_tasks_do_not_get_the_line(self):
        write_task(self.dirs["processing"], "t1")
        write_task(self.dirs["awaiting_credits"], "t0", credit_reset_at=FUTURE)
        self.assertNotIn("Credit hold", server.task_status("t1"))
        out = server.task_status("t0")
        self.assertIn(f"Credit reset: {FUTURE}", out)
        self.assertNotIn("Credit hold", out)


class Marker(unittest.TestCase):
    def test_claim_is_exclusive_and_clear_reports_who_removed_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            at = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)
            self.assertTrue(credit_hold.claim_marker(d, at, "session"))
            self.assertFalse(credit_hold.claim_marker(d, at, "session"))
            self.assertEqual(credit_hold.read_marker(d)["reset_at"], at.isoformat())
            self.assertTrue(credit_hold.clear_marker(d))
            self.assertFalse(credit_hold.clear_marker(d))
            self.assertIsNone(credit_hold.read_marker(d))


if __name__ == "__main__":
    unittest.main()
