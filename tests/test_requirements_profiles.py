"""One manifest, several shapes: a requirement tagged `profiles: [...]` applies
only when the project's requirements_profile is one of them; an untagged one
always applies; a project with no profile set sees every requirement, as before.

    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runner

MANIFEST = """\
version: 1
requirements:
  - id: admin-key
    kind: file
    path: credentials/live-service-account.json
    profiles: [pipeline]
    why: the pipeline writes production Firestore
  - id: geocoding
    kind: env
    vars: [GOOGLE_GEOCODING_API_KEY]
    profiles: [scoped, pipeline]
  - id: always
    kind: file
    path: CLAUDE.md
"""


class RequirementApplies(unittest.TestCase):
    def test_untagged_always_applies(self):
        self.assertEqual(runner.requirement_applies({"kind": "file"}, None), (True, None))
        self.assertEqual(runner.requirement_applies({"kind": "file"}, "scoped"), (True, None))

    def test_tagged_applies_only_to_its_profiles(self):
        req = {"kind": "file", "profiles": ["pipeline"]}
        self.assertEqual(runner.requirement_applies(req, "pipeline"), (True, None))
        self.assertEqual(runner.requirement_applies(req, "scoped"), (False, None))

    def test_no_profile_on_the_project_means_everything_applies(self):
        req = {"kind": "file", "profiles": ["pipeline"]}
        self.assertEqual(runner.requirement_applies(req, None), (True, None))
        self.assertEqual(runner.requirement_applies(req, ""), (True, None))

    def test_malformed_profiles_is_a_problem_not_a_skip(self):
        for bad in ("pipeline", [], [1], [""], {"a": 1}):
            applies, problem = runner.requirement_applies({"profiles": bad}, "scoped")
            self.assertFalse(applies, bad)
            self.assertIn("profiles", problem, bad)


class CheckRequirementsWithProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "repo"
        self.project.mkdir()
        (self.project / runner.MANIFEST_NAME).write_text(MANIFEST)
        (self.project / "CLAUDE.md").write_text("# repo\n")
        mcp_dir = Path(self.tmp.name) / "mcp"
        mcp_dir.mkdir()
        p = mock.patch.object(runner, "MCP_CONFIG_DIR", mcp_dir)
        p.start()
        self.addCleanup(p.stop)

    def check(self, profile, env=None):
        return runner.check_requirements("repo", str(self.project), env or {}, profile)

    def ids(self, failures):
        return [f.splitlines()[0].split(":")[0].lstrip("- ") for f in failures]

    def test_scoped_project_does_not_need_the_admin_key(self):
        self.assertEqual(self.ids(self.check("scoped")), ["geocoding"])
        self.assertEqual(self.check("scoped", {"GOOGLE_GEOCODING_API_KEY": "k"}), [])

    def test_pipeline_project_needs_everything_tagged_for_it(self):
        self.assertEqual(self.ids(self.check("pipeline", {"GOOGLE_GEOCODING_API_KEY": "k"})),
                         ["admin-key"])

    def test_no_profile_set_keeps_todays_behaviour(self):
        self.assertEqual(self.ids(self.check(None)), ["admin-key", "geocoding"])

    def test_untagged_requirement_applies_to_every_profile(self):
        (self.project / "CLAUDE.md").unlink()
        for profile in (None, "scoped", "pipeline", "other"):
            self.assertIn("always", self.ids(self.check(profile, {"GOOGLE_GEOCODING_API_KEY": "k"})),
                          profile)

    def test_unknown_profile_sees_only_untagged_requirements(self):
        self.assertEqual(self.check("other"), [])

    def test_malformed_profiles_fails_the_task_with_a_pointer(self):
        (self.project / runner.MANIFEST_NAME).write_text(
            "requirements:\n  - id: x\n    kind: file\n    path: CLAUDE.md\n    profiles: pipeline\n")
        failures = self.check("scoped")
        self.assertEqual(len(failures), 1)
        self.assertIn("profiles", failures[0])


if __name__ == "__main__":
    unittest.main()
