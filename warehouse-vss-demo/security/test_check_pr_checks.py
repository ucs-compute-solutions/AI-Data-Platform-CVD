"""Synthetic, network-free tests of the read-only pull-request check gate."""
import contextlib
import io
import json
from types import SimpleNamespace
import unittest

import check_pr_checks as gate


SHA = "a" * 40
NEW_SHA = "b" * 40
REPOSITORY = "example/warehouse"


def check(name, app, ident, status="completed", conclusion="success", head=SHA, suite=10):
    value = {"id": ident, "name": name, "app": {"id": app}, "head_sha": head,
             "status": status, "conclusion": conclusion}
    if suite is not None:
        value["check_suite"] = {"id": suite}
    return value


def required():
    return [check(name, app, index) for index, (name, app)
            in enumerate(gate.REQUIRED.items(), 1)]


def status(name, ident, state):
    return {"id": ident, "context": name, "state": state}


class FakeGitHub:
    def __init__(self, runs=None, statuses=None, final_head=SHA,
                 check_pages=None, status_pages=None, fail_call=None):
        runs = required() if runs is None else runs
        self.responses = [[{"head": {"sha": SHA}}],
                          check_pages or [{"total_count": len(runs), "check_runs": runs}],
                          status_pages if status_pages is not None else [statuses or []],
                          [{"head": {"sha": final_head}}]]
        self.calls = []
        self.fail_call = fail_call

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        index = len(self.calls) - 1
        if index > 3:
            raise AssertionError("Unexpected additional API request")
        expected = ("pulls/7", "check-runs?per_page=100&filter=all",
                    "statuses?per_page=100", "pulls/7")[index]
        if not command[-1].endswith(expected):
            raise AssertionError("Unexpected API request")
        if self.fail_call == index:
            return SimpleNamespace(returncode=1, stdout="untrusted server detail",
                                   stderr="untrusted process detail")
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.responses[index]), stderr="")


class CheckGateTests(unittest.TestCase):
    def run_gate(self, fake):
        return gate.run_gate(REPOSITORY, 7, fake)

    def test_all_required_success_is_read_only_and_pinned(self):
        fake = FakeGitHub()
        result = self.run_gate(fake)
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(result["headSha"], SHA)
        self.assertEqual(len(fake.calls), 4)
        for command, kwargs in fake.calls:
            self.assertEqual(command[:2], ["gh", "api"])
            self.assertIn("--paginate", command)
            self.assertIn("--slurp", command)
            self.assertEqual(command[command.index("--method") + 1], "GET")
            self.assertEqual(command[command.index("--hostname") + 1], "github.com")
            self.assertTrue(kwargs["capture_output"])
            self.assertFalse(kwargs["check"])
        for command, _ in fake.calls[1:3]:
            self.assertIn("commits/" + SHA + "/", command[-1])

    def test_original_gitguardian_failure_rejected(self):
        runs = required()
        runs[-1]["conclusion"] = "failure"
        result = self.run_gate(FakeGitHub(runs))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "GitGuardian Security Checks", "conclusion": "failure"}, result["checks"])

    def test_app_spoofing_cannot_satisfy_required_context(self):
        runs = required()
        runs[-1]["app"]["id"] = 999
        result = self.run_gate(FakeGitHub(runs))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "GitGuardian Security Checks", "conclusion": "unexpected-app"}, result["checks"])
        self.assertIn({"name": "GitGuardian Security Checks", "conclusion": "missing"}, result["checks"])

    def test_unexpected_app_rejected_even_with_expected_success(self):
        result = self.run_gate(FakeGitHub(required() + [check("static-validation", 999, 90)]))
        self.assertEqual(result["conclusion"], "failure")

    def test_newer_pending_rerun_cannot_use_older_success(self):
        runs = required() + [check("GitGuardian Security Checks", 46505, 99,
                                   status="queued", conclusion=None)]
        result = self.run_gate(FakeGitHub(runs))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "GitGuardian Security Checks", "conclusion": "pending"}, result["checks"])

    def test_new_success_supersedes_old_failure_with_same_app(self):
        runs = required()
        runs[-1]["conclusion"] = "failure"
        runs.append(check("GitGuardian Security Checks", 46505, 99))
        self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "success")

    def test_independent_suite_failure_cannot_be_hidden_by_new_success(self):
        runs = required() + [check("shared-test", 15368, 100, conclusion="failure", suite=10),
                             check("shared-test", 15368, 101, suite=20)]
        result = self.run_gate(FakeGitHub(runs))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "security-gate", "conclusion": "ambiguous-check-suites"}, result["checks"])

    def test_distinct_suites_fail_closed_even_when_both_succeed(self):
        runs = required() + [check("shared-test", 15368, 100, suite=10),
                             check("shared-test", 15368, 101, suite=20)]
        self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "failure")

    def test_duplicate_context_requires_known_suite_on_every_attempt(self):
        for first, second in ((None, None), (10, None), (None, 10)):
            with self.subTest(first=first, second=second):
                runs = required() + [check("shared-test", 15368, 100, suite=first),
                                     check("shared-test", 15368, 101, suite=second)]
                result = self.run_gate(FakeGitHub(runs))
                self.assertEqual(result["conclusion"], "failure")
                self.assertIn({"name": "security-gate", "conclusion": "ambiguous-check-suites"}, result["checks"])

    def test_single_attempt_without_suite_does_not_require_deduplication(self):
        runs = required() + [check("single-test", 15368, 100, suite=None)]
        self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "success")

    def test_same_suite_pending_attempt_supersedes_success(self):
        runs = required() + [check("shared-test", 15368, 100, suite=30),
                             check("shared-test", 15368, 101, status="in_progress", conclusion=None, suite=30)]
        result = self.run_gate(FakeGitHub(runs))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "shared-test", "conclusion": "pending"}, result["checks"])

    def test_same_suite_success_supersedes_failed_attempt(self):
        runs = required() + [check("shared-test", 15368, 100, conclusion="failure", suite=30),
                             check("shared-test", 15368, 101, suite=30)]
        self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "success")

    def test_changed_head_rejected(self):
        result = self.run_gate(FakeGitHub(final_head=NEW_SHA))
        self.assertEqual(result["conclusion"], "failure")
        self.assertEqual(result["observedHeadSha"], NEW_SHA)
        self.assertIn({"name": "security-gate", "conclusion": "head-changed"}, result["checks"])

    def test_all_check_and_legacy_pages_are_evaluated(self):
        runs = required()
        pages = [{"total_count": 4, "check_runs": runs},
                 {"total_count": 4, "check_runs": [check("other-check", 42, 20)]}]
        statuses = [[status("old-ci", 1, "success")], [status("other-ci", 2, "failure")]]
        result = self.run_gate(FakeGitHub(check_pages=pages, status_pages=statuses))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "other-ci", "conclusion": "failure"}, result["statuses"])
        self.assertIn({"name": "other-check", "conclusion": "success"}, result["checks"])

    def test_missing_check_page_is_rejected(self):
        result = self.run_gate(FakeGitHub(check_pages=[{"total_count": 4, "check_runs": required()}]))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "security-gate", "conclusion": "invalid-api-response"}, result["checks"])

    def test_failure_on_later_check_page_is_rejected(self):
        pages = [{"total_count": 4, "check_runs": required()},
                 {"total_count": 4, "check_runs": [check("later-check", 42, 20, conclusion="failure")]}]
        result = self.run_gate(FakeGitHub(check_pages=pages))
        self.assertEqual(result["conclusion"], "failure")
        self.assertIn({"name": "later-check", "conclusion": "failure"}, result["checks"])

    def test_legacy_failure_rejected(self):
        result = self.run_gate(FakeGitHub(statuses=[status("legacy-ci", 10, "failure")]))
        self.assertEqual(result["conclusion"], "failure")

    def test_latest_legacy_status_per_context_wins(self):
        values = [status("legacy-ci", 9, "failure"), status("legacy-ci", 10, "success")]
        self.assertEqual(self.run_gate(FakeGitHub(statuses=values))["conclusion"], "success")

    def test_legacy_success_cannot_replace_app_bound_check(self):
        values = [status("GitGuardian Security Checks", 10, "success")]
        result = self.run_gate(FakeGitHub(runs=required()[:-1], statuses=values))
        self.assertEqual(result["conclusion"], "failure")

    def test_extra_current_check_must_succeed(self):
        for conclusion in ("failure", "neutral", "skipped", "cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                runs = required() + [check("extra-check", 42, 20, conclusion=conclusion)]
                self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "failure")

    def test_required_check_cannot_be_skipped_or_neutral(self):
        for conclusion in ("skipped", "neutral"):
            runs = required()
            runs[0]["conclusion"] = conclusion
            self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "failure")

    def test_api_failure_is_sanitized(self):
        for index in range(4):
            with self.subTest(index=index):
                result = self.run_gate(FakeGitHub(fail_call=index))
                self.assertEqual(result["conclusion"], "failure")
                self.assertNotIn("untrusted", json.dumps(result))
                self.assertIn({"name": "security-gate", "conclusion": "api-error"}, result["checks"])

    def test_foreign_head_in_check_response_is_rejected(self):
        runs = required()
        runs[0]["head_sha"] = NEW_SHA
        self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "failure")

    def test_duplicate_run_ids_rejected(self):
        runs = required()
        runs.append(dict(runs[0]))
        self.assertEqual(self.run_gate(FakeGitHub(runs))["conclusion"], "failure")

    def test_malformed_states_are_sanitized_failures(self):
        for field in ("status", "conclusion"):
            runs = required()
            runs[0][field] = {"unexpected": "structure"}
            result = self.run_gate(FakeGitHub(runs))
            self.assertEqual(result["conclusion"], "failure")
            self.assertIn({"name": "security-gate", "conclusion": "invalid-api-response"}, result["checks"])
        result = self.run_gate(FakeGitHub(statuses=[status("legacy-ci", 1, [])]))
        self.assertEqual(result["conclusion"], "failure")

    def test_malformed_or_duplicate_json_is_rejected(self):
        for payload in ("not-json", '[{"head": {}, "head": {}}]', '[NaN]', '[]'):
            def runner(*args, **kwargs):
                return SimpleNamespace(returncode=0, stdout=payload, stderr="")
            result = gate.run_gate(REPOSITORY, 7, runner)
            self.assertEqual(result["conclusion"], "failure")
            self.assertIn({"name": "security-gate", "conclusion": "invalid-api-response"}, result["checks"])

    def test_untrusted_names_are_redacted(self):
        for name in ("arbitrary\nannotation", "https://example.org/check", "x" * 30):
            runs = required() + [check(name, 42, 20)]
            result = self.run_gate(FakeGitHub(runs))
            self.assertEqual(result["conclusion"], "success")
            self.assertIn({"name": "[redacted check name]", "conclusion": "success"}, result["checks"])
            self.assertNotIn(name, json.dumps(result))

    def test_invalid_inputs_make_no_api_request(self):
        for repository, pr in (("example/warehouse/extra", 7), ("example/..", 7),
                               (REPOSITORY, 0), (REPOSITORY, True)):
            fake = FakeGitHub()
            self.assertEqual(gate.run_gate(repository, pr, fake)["conclusion"], "failure")
            self.assertEqual(fake.calls, [])

    def test_cli_prints_only_safe_report(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = gate.main(["--repository", REPOSITORY, "--pr", "7"], FakeGitHub())
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"headSha", "conclusion", "checks", "statuses"})
        for row in result["checks"]:
            self.assertEqual(set(row), {"name", "conclusion"})


if __name__ == "__main__":
    unittest.main()
