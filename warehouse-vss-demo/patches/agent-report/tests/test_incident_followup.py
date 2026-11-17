"""Offline regressions for conversation-scoped, incident-bound follow-up answers."""

from __future__ import annotations

import ast
import copy
import importlib.util
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


DIRECTORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "warehouse_incident_followup", DIRECTORY / "incident_followup.py"
)
assert SPEC and SPEC.loader
FOLLOWUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FOLLOWUP)
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "warehouse_incident_followup_validator", DIRECTORY / "validate-incident-followup.py"
)
assert VALIDATOR_SPEC and VALIDATOR_SPEC.loader
VALIDATOR = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR)

INCIDENT_A = "c829080aecc4cc8042be47449efc8b24eba19428"
INCIDENT_B = "61172f4eac18796e5921a08468b895c84041ab40"
START = "2026-11-14T18:17:14.865Z"
END = "2026-11-14T18:17:44.799Z"
ASSESSMENT = "A person without a hard hat is wearing a blue shirt and dark pants."
REPORT = "# Incident report\nA worker without a hard hat walks in the aisle."
COLOR_QUESTION = "what is the color of clothes of person who is not wearing hard hat ?"


def incident(incident_id: str = INCIDENT_A, sensor: str = "Camera_01") -> dict:
    return {
        "Id": incident_id,
        "sensorId": sensor,
        "timestamp": START,
        "end": END,
        "category": "Hard Hat Missing",
        "info": {"reasoning": ASSESSMENT, "verdict": "confirmed"},
    }


def context(incident_id: str = INCIDENT_A, sensor: str = "Camera_01") -> dict:
    return FOLLOWUP.build_context(incident(incident_id, sensor), REPORT)


def supported(answer: str = "The person is wearing a blue shirt and dark pants.",
              quote: str = "blue shirt and dark pants") -> str:
    return json.dumps({"supported": True, "answer": answer, "quotes": [quote]})


class IncidentContextTests(unittest.TestCase):
    def test_valid_context_retains_authoritative_source_identity_and_assessment(self):
        value = context()
        self.assertEqual(value["incident_id"], INCIDENT_A)
        self.assertEqual(value["sensor_id"], "Camera_01")
        self.assertEqual(value["assessment"], ASSESSMENT)
        self.assertEqual(value["report_text"], REPORT)
        self.assertEqual(value["category"], "Hard Hat Missing")
        self.assertEqual(value["verdict"], "confirmed")
        self.assertTrue(value["start_timestamp"].endswith("Z"))
        self.assertTrue(value["end_timestamp"].endswith("Z"))
        self.assertEqual(FOLLOWUP.validated_context(value), value)

    def test_build_does_not_mutate_source_or_retain_its_mutable_containers(self):
        source = incident()
        before = copy.deepcopy(source)
        value = FOLLOWUP.build_context(source, REPORT)
        self.assertEqual(source, before)
        source["info"]["reasoning"] = "A different statement."
        self.assertEqual(value["assessment"], ASSESSMENT)

    def test_reasoning_description_is_available_when_reasoning_is_missing(self):
        source = incident()
        source["info"] = {"reasoningDescription": ASSESSMENT, "verdict": "confirmed"}
        self.assertEqual(FOLLOWUP.build_context(source, REPORT)["assessment"], ASSESSMENT)

    def test_source_identifiers_are_not_inferred_from_report_text(self):
        misleading_report = f"Report for {INCIDENT_B}, Camera_03."
        value = FOLLOWUP.build_context(incident(), misleading_report)
        self.assertEqual(value["incident_id"], INCIDENT_A)
        self.assertEqual(value["sensor_id"], "Camera_01")

    def test_missing_or_malformed_authoritative_identity_fails(self):
        invalid = [
            ("Id", None), ("Id", "unknown"), ("Id", "g" * 40),
            ("Id", "a" * 39), ("Id", "a" * 41),
            ("sensorId", None), ("sensorId", ""),
            ("sensorId", "Camera_01/../Camera_02"),
            ("sensorId", "Camera_01\nCamera_02"),
        ]
        for name, replacement in invalid:
            with self.subTest(name=name, value=replacement):
                source = incident()
                source[name] = replacement
                with self.assertRaises(ValueError):
                    FOLLOWUP.build_context(source, REPORT)

    def test_interval_requires_utc_positive_duration_and_at_most_ten_minutes(self):
        invalid = [
            ("timestamp", "2026-11-14T18:17:14.865"),
            ("timestamp", "not-a-timestamp"),
            ("end", START),
            ("end", "2026-11-14T18:17:13.865Z"),
            ("end", "2026-11-14T18:27:14.866Z"),
            ("end", None),
        ]
        for name, replacement in invalid:
            with self.subTest(name=name, value=replacement):
                source = incident()
                source[name] = replacement
                with self.assertRaises(ValueError):
                    FOLLOWUP.build_context(source, REPORT)

    def test_ten_minute_boundary_is_accepted(self):
        source = incident()
        source["end"] = "2026-11-14T18:27:14.865Z"
        self.assertEqual(FOLLOWUP.build_context(source, REPORT)["incident_id"], INCIDENT_A)

    def test_invalid_restored_state_is_rejected_without_throwing(self):
        invalid = [None, [], "old report", {}, {"incident_id": INCIDENT_A}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(FOLLOWUP.validated_context(value), {})
        value = context()
        value["end_timestamp"] = value["start_timestamp"]
        self.assertEqual(FOLLOWUP.validated_context(value), {})

    def test_camera_identifiers_with_safe_punctuation_are_supported(self):
        for sensor in ("Camera", "Camera_02", "warehouse.camera-1"):
            with self.subTest(sensor=sensor):
                self.assertEqual(context(sensor=sensor)["sensor_id"], sensor)


class FollowupRoutingTests(unittest.TestCase):
    def test_incident_visual_questions_are_recognized(self):
        for question in (
            COLOR_QUESTION,
            "What color clothing is he wearing?",
            "Is the forklift moving or stationary in that clip?",
            "What is the person carrying in this video?",
            "Is the worker wearing a hard hat?",
        ):
            with self.subTest(question=question):
                self.assertTrue(FOLLOWUP.is_visual_followup(question))

    def test_fresh_report_requests_remain_report_requests(self):
        for question in (
            f"Generate a report for incident {INCIDENT_A} with sensor id Camera_01.",
            "Create a detailed report for the last incident at Camera_02.",
            "Regenerate the report for this incident.",
        ):
            with self.subTest(question=question):
                self.assertTrue(FOLLOWUP.is_report_request(question))
                self.assertFalse(FOLLOWUP.is_visual_followup(question))

    def test_counts_of_this_clip_are_not_confused_with_live_occupancy(self):
        self.assertTrue(FOLLOWUP.is_visual_followup("How many people are visible in this clip?"))
        self.assertFalse(FOLLOWUP.is_visual_followup("How many people are in Camera_01?"))

    def test_other_existing_workflows_are_not_visual_followups(self):
        for question in (
            "List all incidents from Camera_01 in the last hour.",
            "How many people are in Camera_01?",
            "Take a snapshot from Camera_01.",
            "Show available cameras.",
            "What is the architecture of this solution?",
        ):
            with self.subTest(question=question):
                self.assertFalse(FOLLOWUP.is_visual_followup(question))


class IncidentFollowupAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_then_color_uses_exact_existing_assessment_without_video(self):
        bound = context()
        complete = AsyncMock(return_value=supported())
        video = AsyncMock()
        result = await FOLLOWUP.answer_followup(bound, COLOR_QUESTION, complete, video)
        self.assertIsInstance(result, str)
        self.assertIn("blue shirt", result)
        self.assertIn("dark pants", result)
        self.assertIn(INCIDENT_A, result)
        self.assertIn("Camera_01", result)
        self.assertTrue(any(label in result.lower() for label in ("assessment", "evidence", "report")))
        complete.assert_awaited_once()
        video.assert_not_awaited()
        system, payload = complete.call_args.args
        self.assertIsInstance(system, str)
        self.assertEqual(payload["question"], COLOR_QUESTION)
        self.assertEqual(payload["evidence"]["assessment"], ASSESSMENT)
        self.assertEqual(payload["evidence"].get("report_text", payload["evidence"].get("report")), REPORT)
        self.assertIn(INCIDENT_A, json.dumps(payload))
        self.assertIn("Camera_01", json.dumps(payload))

    async def test_missing_detail_uses_one_exact_clip_and_the_actual_user_question(self):
        bound = context()
        complete = AsyncMock(return_value=json.dumps({"supported": False, "answer": "", "quotes": []}))
        video = AsyncMock(return_value="The worker is carrying a cardboard box.")
        question = "What is the person carrying in this video?"
        result = await FOLLOWUP.answer_followup(bound, question, complete, video)
        video.assert_awaited_once()
        payload = video.call_args.args[0]
        self.assertEqual(payload["sensor_id"], bound["sensor_id"])
        self.assertEqual(payload["start_timestamp"], bound["start_timestamp"])
        self.assertEqual(payload["end_timestamp"], bound["end_timestamp"])
        self.assertIn(question, payload["user_prompt"])
        self.assertNotIn("object_ids", payload)
        self.assertTrue(set(payload).issubset({
            "sensor_id", "start_timestamp", "end_timestamp", "user_prompt", "vlm_reasoning"
        }))
        self.assertIn("cardboard box", result)
        self.assertIn(INCIDENT_A, result)
        self.assertIn("Camera_01", result)
        self.assertTrue(any(label in result.lower() for label in ("video", "clip", "visual")))

    async def test_unsupported_or_malformed_quoted_answer_cannot_skip_video(self):
        invalid = [
            "not JSON",
            json.dumps({"supported": False, "answer": "blue", "quotes": ["blue shirt"]}),
            json.dumps({"supported": "true", "answer": "blue", "quotes": ["blue shirt"]}),
            json.dumps({"supported": True, "answer": "blue", "quotes": []}),
            json.dumps({"supported": True, "answer": "red clothes", "quotes": ["red clothes"]}),
            json.dumps({"supported": True, "answer": "", "quotes": ["blue shirt"]}),
            json.dumps({"supported": True, "answer": "blue", "quotes": "blue shirt"}),
        ]
        for response in invalid:
            with self.subTest(response=response):
                complete = AsyncMock(return_value=response)
                video = AsyncMock(return_value="Clothing color is not clear in this clip.")
                result = await FOLLOWUP.answer_followup(context(), COLOR_QUESTION, complete, video)
                video.assert_awaited_once()
                self.assertIn("not clear", result)

    async def test_evidence_completion_failure_falls_back_to_exact_clip(self):
        complete = AsyncMock(side_effect=TimeoutError("completion timeout"))
        video = AsyncMock(return_value="The person is wearing a blue shirt.")
        result = await FOLLOWUP.answer_followup(context(), COLOR_QUESTION, complete, video)
        video.assert_awaited_once()
        self.assertIn("blue shirt", result)

    async def test_unavailable_clip_is_reported_without_selecting_another_incident(self):
        complete = AsyncMock(return_value=json.dumps({"supported": False, "answer": "", "quotes": []}))
        video = AsyncMock(side_effect=RuntimeError("404 clip unavailable"))
        result = await FOLLOWUP.answer_followup(context(), COLOR_QUESTION, complete, video)
        video.assert_awaited_once()
        self.assertIsInstance(result, str)
        self.assertTrue(any(word in result.lower() for word in ("unavailable", "unable", "could not", "cannot")))
        self.assertNotIn(INCIDENT_B, result)
        self.assertNotIn("Camera_03", result)

    async def test_no_context_does_not_adopt_the_latest_incident(self):
        for bound in ({}, None, {"incident_id": INCIDENT_A}):
            with self.subTest(bound=bound):
                complete, video = AsyncMock(), AsyncMock()
                self.assertIsNone(await FOLLOWUP.answer_followup(bound, COLOR_QUESTION, complete, video))
                complete.assert_not_awaited()
                video.assert_not_awaited()

    async def test_different_camera_or_incident_does_not_silently_switch(self):
        for question in (
            "What color clothes is the person wearing in Camera_03?",
            f"What color clothes is the person wearing in incident {INCIDENT_B}?",
        ):
            with self.subTest(question=question):
                complete, video = AsyncMock(), AsyncMock()
                result = await FOLLOWUP.answer_followup(context(), question, complete, video)
                self.assertIsInstance(result, str)
                self.assertTrue(any(word in result.lower() for word in ("different", "bound", "select", "report", "match")))
                complete.assert_not_awaited()
                video.assert_not_awaited()

    async def test_same_explicit_camera_keeps_bound_incident(self):
        complete = AsyncMock(return_value=supported())
        video = AsyncMock()
        result = await FOLLOWUP.answer_followup(
            context(), "What color clothes is the person wearing in Camera_01?", complete, video
        )
        self.assertIn(INCIDENT_A, result)
        complete.assert_awaited_once()
        video.assert_not_awaited()

    async def test_referential_this_camera_does_not_count_as_a_different_sensor(self):
        complete = AsyncMock(return_value=supported())
        video = AsyncMock()
        result = await FOLLOWUP.answer_followup(
            context(), "What color clothes is the person wearing in this camera?", complete, video
        )
        self.assertIn("blue shirt", result)
        complete.assert_awaited_once()
        video.assert_not_awaited()

    async def test_contexts_for_two_chats_cannot_contaminate_each_other(self):
        bound_a = context()
        source_b = incident(INCIDENT_B, "Camera_03")
        source_b["info"]["reasoning"] = "The person without a hard hat wears a yellow jacket."
        bound_b = FOLLOWUP.build_context(source_b, "A different incident report.")
        before_a, before_b = copy.deepcopy(bound_a), copy.deepcopy(bound_b)
        complete_a = AsyncMock(return_value=supported())
        complete_b = AsyncMock(return_value=supported("The person wears a yellow jacket.", "yellow jacket"))
        video_a, video_b = AsyncMock(), AsyncMock()
        answer_a = await FOLLOWUP.answer_followup(bound_a, COLOR_QUESTION, complete_a, video_a)
        answer_b = await FOLLOWUP.answer_followup(bound_b, COLOR_QUESTION, complete_b, video_b)
        self.assertIn(INCIDENT_A, answer_a)
        self.assertNotIn(INCIDENT_B, answer_a)
        self.assertIn("blue shirt", answer_a)
        self.assertIn(INCIDENT_B, answer_b)
        self.assertNotIn(INCIDENT_A, answer_b)
        self.assertIn("yellow jacket", answer_b)
        self.assertNotIn(ASSESSMENT, json.dumps(complete_b.call_args.args[1]))
        self.assertEqual(bound_a, before_a)
        self.assertEqual(bound_b, before_b)
        video_a.assert_not_awaited()
        video_b.assert_not_awaited()

    async def test_unrelated_and_report_requests_do_not_invoke_followup_models(self):
        for question in (
            f"Generate a report for incident {INCIDENT_A} with sensor id Camera_01.",
            "How many people are in Camera_01?",
            "Take a snapshot from Camera_01.",
            "What is the architecture of this solution?",
        ):
            with self.subTest(question=question):
                complete, video = AsyncMock(), AsyncMock()
                self.assertIsNone(await FOLLOWUP.answer_followup(context(), question, complete, video))
                complete.assert_not_awaited()
                video.assert_not_awaited()


def added_patch_block(start: str) -> str:
    """Read one contiguous added source block from the checked-in patch."""
    lines = (DIRECTORY / "0007-incident-bound-followup.patch").read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("+") and line[1:].strip() == start)
    result = []
    for line in lines[index:]:
        if not line.startswith("+") or line.startswith("+++"):
            break
        result.append(line[1:])
    return textwrap.dedent("\n".join(result)).strip()


class TopAgentPatchContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the exact new dispatch/state branches, without importing NAT.

    These are patch integration contracts, not a full LangGraph or browser test.
    A live report followed by a question remains a separate acceptance check.
    """

    @classmethod
    def setUpClass(cls):
        cls.patch = (DIRECTORY / "0007-incident-bound-followup.patch").read_text()
        block = added_patch_block("if is_report_request(question):")
        source = (
            "async def dispatch(self, state, question):\n"
            + textwrap.indent(block, "    ")
            + "\n    state.generic_planning_reached = True\n    return state\n"
        )
        scope = {"is_report_request": FOLLOWUP.is_report_request, "logger": Mock()}
        exec(compile(ast.parse(source), "top_agent_followup_dispatch", "exec"), scope)
        cls.dispatch = staticmethod(scope["dispatch"])
        cls.metadata_block = ast.parse(added_patch_block(
            'if tool_name in ("report_agent", "multi_report_agent") and agent_output.status == "success":'
        ))

    def test_state_binding_uses_per_conversation_default_factory(self):
        line = next(
            line[1:].strip() for line in self.patch.splitlines()
            if line.startswith("+") and "bound_incident: dict[str, str] = Field(" in line
        )
        assignment = ast.parse(line).body[0]
        self.assertIsInstance(assignment, ast.AnnAssign)
        self.assertEqual(assignment.target.id, "bound_incident")
        factory = next(keyword.value for keyword in assignment.value.keywords if keyword.arg == "default_factory")
        self.assertIsInstance(factory, ast.Name)
        self.assertEqual(factory.id, "dict")

    def test_new_turn_restores_a_fresh_validated_conversation_binding(self):
        line = next(
            line[1:].strip() for line in self.patch.splitlines()
            if line.startswith("+") and "bound_incident=validated_context(previous_state.get(" in line
        )
        call = ast.parse("restore(" + line + ")", mode="eval").body
        value = next(keyword.value for keyword in call.keywords if keyword.arg == "bound_incident")
        previous = context()
        restored = eval(compile(ast.Expression(value), "top_agent_binding_restore", "eval"), {
            "validated_context": FOLLOWUP.validated_context,
            "previous_state": {"bound_incident": previous},
        })
        self.assertEqual(restored, previous)
        self.assertIsNot(restored, previous)
        restored["assessment"] = "another response"
        self.assertEqual(previous["assessment"], ASSESSMENT)

    def apply_metadata(self, tool_name, bound, metadata, status="success"):
        state = SimpleNamespace(bound_incident=bound)
        scope = {
            "tool_name": tool_name,
            "agent_output": SimpleNamespace(status=status, metadata=metadata),
            "state": state,
            "validated_context": FOLLOWUP.validated_context,
        }
        exec(compile(self.metadata_block, "top_agent_context_metadata", "exec"), scope)
        return state.bound_incident

    def test_successful_report_replaces_the_prior_incident_with_validated_metadata(self):
        first, second = context(), context(INCIDENT_B, "Camera_03")
        result = self.apply_metadata("report_agent", first, {"incident_context": second})
        self.assertEqual(result, second)
        self.assertIsNot(result, second)

    def test_invalid_successful_report_binding_does_not_keep_stale_context(self):
        result = self.apply_metadata("report_agent", context(), {"incident_context": {"sensor_id": "Camera_03"}})
        self.assertEqual(result, {})

    def test_multi_incident_results_clear_the_old_single_incident(self):
        result = self.apply_metadata("multi_report_agent", context(), {"incident_context": context()})
        self.assertEqual(result, {})

    def test_unrelated_subagent_does_not_adopt_incident_metadata(self):
        original = context()
        result = self.apply_metadata("another_agent", original, {"incident_context": context(INCIDENT_B, "Camera_03")})
        self.assertEqual(result, original)

    def test_binding_is_transmitted_as_metadata_not_visible_side_effect(self):
        block = added_patch_block('"incident_context": incident_context,')
        self.assertIn('"incident_context": incident_context', block)
        self.assertIn('metadata.get("incident_context")', self.patch)
        self.assertNotIn('side_effects["incident_context"]', self.patch)

    async def test_new_report_request_clears_stale_binding_before_generic_planning(self):
        agent = SimpleNamespace(_answer_incident_followup=AsyncMock())
        state = SimpleNamespace(bound_incident=context(), final_answer="")
        result = await self.dispatch(agent, state, f"Generate a report for incident {INCIDENT_B}.")
        self.assertEqual(result.bound_incident, {})
        self.assertTrue(result.generic_planning_reached)
        agent._answer_incident_followup.assert_not_awaited()

    async def test_bound_answer_returns_before_the_generic_clarifying_planner(self):
        agent = SimpleNamespace(_answer_incident_followup=AsyncMock(return_value="A blue shirt."))
        state = SimpleNamespace(bound_incident=context(), final_answer="")
        result = await self.dispatch(agent, state, COLOR_QUESTION)
        self.assertEqual(result.final_answer, "A blue shirt.")
        self.assertFalse(hasattr(result, "generic_planning_reached"))
        agent._answer_incident_followup.assert_awaited_once_with(state, COLOR_QUESTION)

    async def test_unhandled_question_reaches_original_planning(self):
        agent = SimpleNamespace(_answer_incident_followup=AsyncMock(return_value=None))
        state = SimpleNamespace(bound_incident=context(), final_answer="")
        result = await self.dispatch(agent, state, "Show available cameras.")
        self.assertTrue(result.generic_planning_reached)
        self.assertEqual(result.final_answer, "")


class FollowupAcceptanceValidatorTests(unittest.TestCase):
    """Validate acceptance checks with fixed responses; no network/inference."""

    REPORT_RESPONSE = f"Report for {INCIDENT_A}, Camera_01. [PDF Report](https://warehouse.example/report.pdf)"
    MISMATCH_RESPONSE = "Select the other incident's report first; I have not switched cameras."

    def run_validator(self, answer, *, separate="Which incident or camera do you mean?",
                      report=None, other=None, expected_word=None, execute=True):
        arguments = ["validate-incident-followup.py", "--incident-id", INCIDENT_A, "--sensor", "Camera_01"]
        if execute:
            arguments.append("--execute")
        if expected_word:
            arguments.extend(["--expected-word", expected_word])
        responses = [self.REPORT_RESPONSE if report is None else report, answer,
                     self.MISMATCH_RESPONSE if other is None else other, separate]
        with (
            patch("sys.argv", arguments),
            patch.object(VALIDATOR, "chat", side_effect=responses) as chat,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = VALIDATOR.main()
        return result, chat, output.getvalue()

    def test_stored_evidence_source_passes_without_requiring_video(self):
        answer = f"A blue shirt. Source: stored incident assessment/report (Camera_01, {INCIDENT_A})."
        status, calls, output = self.run_validator(answer, expected_word="blue")
        self.assertEqual(status, 0)
        self.assertIn('"check": "report_to_followup", "status": "pass"', output)
        self.assertEqual(calls.call_count, 4)
        report_chat, followup_chat, mismatch_chat, isolated_chat = [call.args[1] for call in calls.call_args_list]
        self.assertEqual(report_chat, followup_chat)
        self.assertEqual(report_chat, mismatch_chat)
        self.assertNotEqual(report_chat, isolated_chat)

    def test_valid_same_clip_source_is_also_a_successful_followup(self):
        answer = f"A blue shirt. Source: analysis of the same incident clip (Camera_01, {INCIDENT_A})."
        self.assertEqual(self.run_validator(answer)[0], 0)

    def test_generic_clarification_is_not_mistaken_for_success(self):
        with self.assertRaisesRegex(ValueError, "not routed"):
            self.run_validator("Which specific incident or sensor are you referring to?")

    def test_mismatched_incident_or_camera_is_rejected(self):
        for answer in (
            f"Blue. Source: stored incident assessment/report (Camera_01, {INCIDENT_B}).",
            f"Blue. Source: stored incident assessment/report (Camera_03, {INCIDENT_A}).",
        ):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, "identity"):
                self.run_validator(answer)

    def test_missing_expected_source_term_fails(self):
        answer = f"Cannot determine. Source: analysis of the same incident clip (Camera_01, {INCIDENT_A})."
        with self.assertRaisesRegex(ValueError, "expected source-supported term"):
            self.run_validator(answer, expected_word="blue")

    def test_new_chat_cannot_inherit_either_bound_source_type(self):
        answer = f"A blue shirt. Source: stored incident assessment/report (Camera_01, {INCIDENT_A})."
        for separate in (
            f"The incident is {INCIDENT_A}.",
            "Source: stored incident assessment/report (a previous camera).",
            "Source: analysis of the same incident clip (a previous camera).",
        ):
            with self.subTest(separate=separate), self.assertRaisesRegex(ValueError, "inherited incident context"):
                self.run_validator(answer, separate=separate)

    def test_report_must_first_return_its_own_identity_and_artifact(self):
        with self.assertRaisesRegex(ValueError, "Fresh report"):
            self.run_validator("unused", report="Report generated, but no matching artifact.")

    def test_plan_only_does_not_call_agent(self):
        status, calls, output = self.run_validator("unused", execute=False)
        self.assertEqual(status, 0)
        calls.assert_not_called()
        self.assertIn("PLAN ONLY", output)


if __name__ == "__main__":
    unittest.main()
