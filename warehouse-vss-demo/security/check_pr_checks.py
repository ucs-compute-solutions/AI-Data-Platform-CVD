#!/usr/bin/env python3
"""Read-only, app-bound check gate for an exact pull-request head.

Uses credentials already managed by gh. Never merges, reruns a check, changes
repository settings, or prints API bodies, annotations, URLs or process errors.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


REQUIRED = {
    "static-validation": 15368,
    "warehouse-security-tests": 15368,
    "GitGuardian Security Checks": 46505,
}
CHECK_STATUSES = {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
CHECK_CONCLUSIONS = {"success", "failure", "neutral", "cancelled", "timed_out",
                     "action_required", "stale", "startup_failure", "skipped"}
LEGACY_STATES = {"success", "failure", "error", "pending"}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class GateError(ValueError):
    """A fixed, safe failure classification; never includes API response text."""


def require(condition):
    if not condition:
        raise GateError("invalid-api-response")


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def invalid_constant(_):
    raise GateError("invalid-api-response")


def positive_integer(value):
    return type(value) is int and value > 0


def valid_repository(value):
    return (isinstance(value, str) and len(value) <= 255
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?/[A-Za-z0-9_.-]+", value)
            and value.rsplit("/", 1)[1] not in {".", ".."})


def safe_name(value):
    """Only human-readable labels leave the gate; redact credential-like names."""
    if value in REQUIRED:
        return value
    if (not isinstance(value, str) or len(value) > 120
            or "://" in value
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/()\-]*", value)
            or re.search(r"[A-Za-z0-9_-]{24,}|(?:password|passwd|token|secret|api.?key)\s*[:=]", value, re.I)):
        return "[redacted check name]"
    return value


class GitHub:
    def __init__(self, repository, runner=None):
        self.repository = repository
        self.runner = runner or subprocess.run

    def pages(self, suffix):
        command = ["gh", "api", "--hostname", "github.com", "--method", "GET",
                   "--paginate", "--slurp", "--header", "Accept: application/vnd.github+json",
                   "repos/" + self.repository + "/" + suffix]
        try:
            result = self.runner(command, stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=90, check=False)
        except (OSError, subprocess.SubprocessError):
            raise GateError("api-error") from None
        if result.returncode != 0:
            raise GateError("api-error")
        try:
            require(isinstance(result.stdout, str)
                    and len(result.stdout.encode("utf-8")) <= MAX_RESPONSE_BYTES)
            pages = json.loads(result.stdout, object_pairs_hook=unique_object,
                               parse_constant=invalid_constant)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise GateError("invalid-api-response") from None
        require(isinstance(pages, list) and bool(pages))
        return pages

    def head(self, pr):
        pages = self.pages("pulls/" + str(pr))
        require(len(pages) == 1 and isinstance(pages[0], dict))
        head = pages[0].get("head")
        require(isinstance(head, dict) and isinstance(head.get("sha"), str)
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head["sha"]))
        return head["sha"]

    def checks(self, head):
        pages = self.pages("commits/" + head + "/check-runs?per_page=100&filter=all")
        runs, expected = [], None
        for page in pages:
            require(isinstance(page, dict) and isinstance(page.get("check_runs"), list)
                    and type(page.get("total_count")) is int and page["total_count"] >= 0)
            if expected is None:
                expected = page["total_count"]
            require(page["total_count"] == expected)
            runs.extend(page["check_runs"])
        require(len(runs) == expected)
        return runs

    def statuses(self, head):
        pages = self.pages("commits/" + head + "/statuses?per_page=100")
        require(all(isinstance(page, list) for page in pages))
        return [item for page in pages for item in page]


def evaluate_checks(runs, head):
    latest, seen, suites = {}, set(), {}
    for run in runs:
        require(isinstance(run, dict) and positive_integer(run.get("id")))
        require(run["id"] not in seen)
        seen.add(run["id"])
        name, app = run.get("name"), run.get("app")
        require(isinstance(name, str) and 0 < len(name) <= 255
                and isinstance(app, dict) and positive_integer(app.get("id"))
                and run.get("head_sha") == head
                and isinstance(run.get("status"), str) and run["status"] in CHECK_STATUSES
                and (run.get("conclusion") is None
                     or (isinstance(run["conclusion"], str) and run["conclusion"] in CHECK_CONCLUSIONS)))
        key = name, app["id"]
        suite = run.get("check_suite")
        require(suite is None or (isinstance(suite, dict) and positive_integer(suite.get("id"))))
        suite_id = suite["id"] if suite is not None else None
        if key in suites and (suite_id is None or suites[key] is None or suites[key] != suite_id):
            # Identical job names can belong to independent workflows. Do not
            # let the newer workflow's success erase another suite's failure.
            raise GateError("ambiguous-check-suites")
        suites[key] = suite_id
        # GitHub run IDs identify creation order, including queued reruns whose
        # started_at is still null. Supersession requires the same known suite.
        if key not in latest or run["id"] > latest[key]["id"]:
            latest[key] = run
    rows, passed = [], True
    for (name, app_id), run in sorted(latest.items()):
        conclusion = run.get("conclusion") if run["status"] == "completed" else "pending"
        conclusion = conclusion or "pending"
        if name in REQUIRED and app_id != REQUIRED[name]:
            conclusion = "unexpected-app"
        rows.append({"name": safe_name(name), "conclusion": conclusion})
        passed = passed and conclusion == "success"
    for name, app_id in REQUIRED.items():
        if (name, app_id) not in latest:
            rows.append({"name": name, "conclusion": "missing"})
            passed = False
    return rows, passed


def evaluate_statuses(statuses):
    latest, seen = {}, set()
    for status in statuses:
        require(isinstance(status, dict) and positive_integer(status.get("id")))
        require(status["id"] not in seen)
        seen.add(status["id"])
        name = status.get("context")
        require(isinstance(name, str) and 0 < len(name) <= 255
                and isinstance(status.get("state"), str) and status["state"] in LEGACY_STATES)
        # Legacy status APIs have no app ID; their latest value per context is
        # checked, but cannot satisfy any of the app-bound required checks.
        if name not in latest or status["id"] > latest[name]["id"]:
            latest[name] = status
    rows = [{"name": safe_name(name), "conclusion": item["state"]}
            for name, item in sorted(latest.items())]
    return rows, all(row["conclusion"] == "success" for row in rows)


def run_gate(repository, pr, runner=None):
    report = {"headSha": None, "conclusion": "failure", "checks": [], "statuses": []}
    try:
        if not valid_repository(repository) or not positive_integer(pr):
            raise GateError("invalid-input")
        github = GitHub(repository, runner)
        head = github.head(pr)
        report["headSha"] = head
        runs, statuses = github.checks(head), github.statuses(head)
        report["checks"], checks_ok = evaluate_checks(runs, head)
        report["statuses"], statuses_ok = evaluate_statuses(statuses)
        observed = github.head(pr)
        if observed != head:
            report["observedHeadSha"] = observed
            raise GateError("head-changed")
        if checks_ok and statuses_ok:
            report["conclusion"] = "success"
    except GateError as error:
        report["checks"].append({"name": "security-gate", "conclusion": str(error)})
    return report


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--pr", required=True, help="Positive pull-request number")
    args = parser.parse_args(argv)
    pr = int(args.pr) if re.fullmatch(r"[0-9]{1,10}", args.pr) else 0
    report = run_gate(args.repository, pr, runner)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["conclusion"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
