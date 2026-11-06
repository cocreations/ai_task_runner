"""A task picks its model with a `/model <id>;` line; an account can set a
default. The runner takes the line out of the prompt and passes `--model` to
the CLI — headless Claude Code ignores a slash command inside a prompt, so
before this every task ran on the CLI default whatever it asked for.

    python3 -m unittest discover -s tests -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import frontmatter

import runner
import server

BUILD_PROMPT = (
    "/model claude-opus-5-5;\n"
    "\n"
    "/moment-build-signup\n"
    "<MOMENT_AI_CONTEXT>\n{\"appId\":\"abc\"}\n</MOMENT_AI_CONTEXT>"
)


class SplitModelDirective(unittest.TestCase):
    def test_first_line_directive_is_taken_out_with_its_blank_line(self):
        model, prompt = runner.split_model_directive(BUILD_PROMPT)
        self.assertEqual(model, "claude-opus-5-5")
        self.assertTrue(prompt.startswith("/moment-build-signup\n<MOMENT_AI_CONTEXT>"))
        self.assertNotIn("/model", prompt)

    def test_semicolon_and_padding_are_optional(self):
        for line in ("/model sonnet", "/model sonnet;", "  /model   sonnet ;  ", "\t/model sonnet;"):
            model, prompt = runner.split_model_directive(line + "\n\nDo the thing.")
            self.assertEqual(model, "sonnet", line)
            self.assertEqual(prompt, "Do the thing.", line)

    def test_ids_with_dots_brackets_and_dates(self):
        for mid in ("claude-opus-5-5", "claude-sonnet-4-5-20250929", "claude-opus-5-5[1m]",
                    "us.anthropic.claude-opus-5-5-v1:0", "opus"):
            self.assertEqual(runner.split_model_directive(f"/model {mid};\n\nx")[0], mid)

    def test_directive_below_other_lines_keeps_them(self):
        model, prompt = runner.split_model_directive("Context line\n/model haiku;\n\nDo it.")
        self.assertEqual(model, "haiku")
        self.assertEqual(prompt, "Context line\nDo it.")

    def test_only_the_first_ten_lines_count(self):
        late = "\n".join(["line"] * 10 + ["/model opus;", "", "Do it."])
        self.assertEqual(runner.split_model_directive(late), (None, late))
        tenth = "\n".join(["line"] * 9 + ["/model opus;", "", "Do it."])
        self.assertEqual(runner.split_model_directive(tenth)[0], "opus")

    def test_no_directive_leaves_the_prompt_alone(self):
        for prompt in ("Do the thing.", "Please use /model opus for this.", "/models are great",
                       "/model", "/model ;"):
            self.assertEqual(runner.split_model_directive(prompt), (None, prompt), prompt)

    def test_a_line_that_names_no_usable_id_is_not_a_directive(self):
        for prompt in ("/model a b\n\nDo it.", "/model --dangerously-skip-permissions\n\nDo it.",
                       "/model -p;\n\nDo it.", "/model " + "x" * 101 + ";\n\nDo it.",
                       "/model $(id);\n\nDo it."):
            self.assertEqual(runner.split_model_directive(prompt), (None, prompt), prompt)

    def test_only_the_first_directive_is_taken(self):
        model, prompt = runner.split_model_directive("/model opus;\n\n/model sonnet;\nDo it.")
        self.assertEqual(model, "opus")
        self.assertEqual(prompt, "/model sonnet;\nDo it.")

    def test_has_model_override(self):
        self.assertTrue(runner.has_model_override(BUILD_PROMPT))
        self.assertFalse(runner.has_model_override("Do the thing."))


class ResolveModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)
        p = mock.patch.object(runner, "CONFIG_DIR", self.config)
        p.start()
        self.addCleanup(p.stop)

    def test_nothing_set_means_the_cli_default(self):
        self.assertEqual(runner.resolve_model("Do it."), (None, "Do it."))

    def test_account_default_applies_when_the_task_names_none(self):
        (self.config / ".default_model").write_text("claude-sonnet-5-5\n")
        self.assertEqual(runner.resolve_model("Do it."), ("claude-sonnet-5-5", "Do it."))

    def test_the_task_beats_the_account_default(self):
        (self.config / ".default_model").write_text("claude-sonnet-5-5\n")
        model, prompt = runner.resolve_model(BUILD_PROMPT)
        self.assertEqual(model, "claude-opus-5-5")
        self.assertNotIn("/model", prompt)

    def test_an_unusable_default_is_ignored(self):
        for bad in ("", "   \n", "--resume", "two words"):
            (self.config / ".default_model").write_text(bad)
            self.assertEqual(runner.resolve_model("Do it."), (None, "Do it."), bad)


class ClaudeCommand(unittest.TestCase):
    def test_model_goes_to_the_cli_as_a_flag(self):
        cmd = runner.claude_command("--session-id", "sid", "the prompt", "claude-opus-5-5")
        self.assertEqual(cmd[:5], ["claude", "--session-id", "sid", "-p", "the prompt"])
        at = cmd.index("--model")
        self.assertEqual(cmd[at + 1], "claude-opus-5-5")

    def test_no_model_no_flag(self):
        self.assertNotIn("--model", runner.claude_command("--session-id", "sid", "p"))
        self.assertNotIn("--model", runner.claude_command("--resume", "sid", "p", None))

    def test_a_resumed_task_keeps_its_model(self):
        cmd = runner.claude_command("--resume", "sid", "p", "opus")
        self.assertEqual(cmd[1:3], ["--resume", "sid"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_mcp_config_only_when_the_file_exists(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "p.mcp.json"
            self.assertNotIn("--mcp-config", runner.claude_command("--session-id", "s", "p", "opus", missing))
            missing.write_text("{}")
            cmd = runner.claude_command("--session-id", "s", "p", "opus", missing)
            self.assertEqual(cmd[-3:], ["--mcp-config", str(missing), "--strict-mcp-config"])
            self.assertIn("--model", cmd)


class RunTaskPassesTheModel(unittest.TestCase):
    """run_task end to end with the CLI stubbed: the directive leaves the
    prompt, --model reaches the command, and the task records what it ran on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project_dir = root / "repo"
        self.project_dir.mkdir()
        self.logs = root / "logs"
        self.logs.mkdir()
        self.config = root / "config"
        self.config.mkdir()
        self.processing = root / "processing"
        self.processing.mkdir()
        self.calls = []
        self.finalized = []

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "claude":
                self.calls.append(cmd)
                kwargs["stdout"].write('{"type":"result","result":"ok"}\n')
                return mock.Mock(returncode=0)
            return mock.Mock(returncode=0, stdout="1\t.\n")

        patches = [
            mock.patch.object(runner, "LOGS_DIR", self.logs),
            mock.patch.object(runner, "CONFIG_DIR", self.config),
            mock.patch.object(runner, "MCP_CONFIG_DIR", root / "mcp"),
            mock.patch.object(runner, "log", lambda *_: None),
            mock.patch.object(runner, "refresh_git_credentials", lambda: None),
            mock.patch.object(runner, "ensure_workspace_ownership", lambda *_: None),
            mock.patch.object(runner, "load_skills", lambda *_: ""),
            mock.patch.object(runner, "check_requirements", lambda *a, **k: []),
            mock.patch.object(runner, "download_attachments", lambda *a, **k: ""),
            mock.patch.object(runner, "cleanup_attachments", lambda *a, **k: None),
            mock.patch.object(runner, "finalize_task",
                              lambda path, status, **kw: self.finalized.append((status, kw))),
            mock.patch.object(runner.subprocess, "run", fake_run),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def task(self, prompt):
        path = self.processing / "t1.md"
        post = frontmatter.Post(prompt, id="t1", project="repo", user="alice",
                                claude_session_id="11111111-1111-1111-1111-111111111111",
                                session_started=False)
        path.write_text(frontmatter.dumps(post))
        return path

    def run_it(self, prompt):
        path = self.task(prompt)
        projects = {"repo": {"directory": str(self.project_dir), "allowed_users": ["alice"]}}
        runner.run_task(path, projects, {})
        self.assertEqual(len(self.calls), 1, "the CLI ran once")
        cmd = self.calls[0]
        return cmd, cmd[cmd.index("-p") + 1], frontmatter.load(str(path)).metadata

    def test_directive_becomes_the_flag_and_leaves_the_prompt(self):
        cmd, sent, meta = self.run_it(BUILD_PROMPT)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5")
        self.assertNotIn("/model", sent)
        self.assertIn("## Task\n/moment-build-signup\n<MOMENT_AI_CONTEXT>", sent)
        self.assertEqual(meta["model"], "claude-opus-5-5")
        self.assertEqual(self.finalized[0][0], "done")

    def test_no_directive_and_no_default_runs_on_the_cli_default(self):
        cmd, sent, meta = self.run_it("Do the thing.")
        self.assertNotIn("--model", cmd)
        self.assertIn("## Task\nDo the thing.", sent)
        self.assertNotIn("model", meta)

    def test_account_default_is_passed_as_the_flag_not_written_into_the_prompt(self):
        (self.config / ".default_model").write_text("claude-sonnet-5-5")
        cmd, sent, meta = self.run_it("Do the thing.")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        self.assertNotIn("/model", sent)
        self.assertEqual(meta["model"], "claude-sonnet-5-5")

    def test_the_stored_task_keeps_its_original_prompt(self):
        path = self.task(BUILD_PROMPT)
        projects = {"repo": {"directory": str(self.project_dir), "allowed_users": ["alice"]}}
        runner.run_task(path, projects, {})
        self.assertTrue(frontmatter.load(str(path)).content.startswith("/model claude-opus-5-5;"))


class TaskStatusShowsTheModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dirs = {name: root / name for name in server.STATUS_DIRS}
        for d in self.dirs.values():
            d.mkdir(parents=True)
        patches = [mock.patch.object(server, "STATUS_DIRS", self.dirs),
                   mock.patch.object(server, "get_current_user", lambda: "alice"),
                   mock.patch.object(server, "_projects", {"repo": {"allowed_users": ["alice"]}})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, **meta):
        post = frontmatter.Post("/model claude-opus-5-5;\n\nModel: not-this-one", id="t1",
                                project="repo", user="alice", status="done", **meta)
        (self.dirs["done"] / "t1.md").write_text(frontmatter.dumps(post))

    def header(self):
        return server.task_status("t1").split("--- Prompt ---")[0]

    def test_model_line_when_the_runner_passed_one(self):
        self.write(model="claude-opus-5-5")
        self.assertIn("\nModel: claude-opus-5-5\n", self.header())

    def test_no_model_line_when_the_cli_chose(self):
        self.write()
        self.assertNotIn("Model:", self.header())


if __name__ == "__main__":
    unittest.main()
