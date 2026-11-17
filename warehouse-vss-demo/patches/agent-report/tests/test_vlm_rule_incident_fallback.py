"""Offline checks for the exact-ID Warehouse VA MCP incident fallback.

The tested search body is extracted from the patch itself. No cluster,
Elasticsearch server, model, or vendor Python dependency is required.
"""

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import textwrap
import unittest


PATCH = Path(__file__).resolve().parents[1] / "0003-find-vlm-only-rule-incidents.patch"
INCIDENT_ID = "example-ui-rule-incident-12345678"


def load_lookup():
    lines = []
    inside = False
    for line in PATCH.read_text(encoding="utf-8").splitlines():
        if line == "         # Query for single incident":
            inside = True
        if inside:
            if line.startswith((" ", "+")) and not line.startswith("+++"):
                lines.append(line[1:])
            if line == "         return incidents[0] if incidents else {}":
                break
    if not lines:
        raise ValueError("exact-ID search body is missing from the patch")
    body = textwrap.dedent("\n".join(lines))
    source = (
        "async def lookup(es_client, _config, index_key, query, incident_fields):\n"
        + textwrap.indent(body, "    ")
    )
    namespace = {}
    exec(compile(ast.parse(source), str(PATCH), "exec"), namespace)
    return namespace["lookup"]


class FakeES:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class VLMRuleIncidentFallbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.lookup = staticmethod(load_lookup())

    async def call_lookup(self, client, *, vlm_verified=False):
        self.query = {"query": {"term": {"Id.keyword": INCIDENT_ID}}}
        self.fields = ["Id", "id", "timestamp", "end", "sensorId", "objectIds", "info"]
        return await self.lookup(
            client, SimpleNamespace(vlm_verified=vlm_verified),
            "vlm_incidents" if vlm_verified else "incidents", self.query, self.fields,
        )

    async def test_existing_raw_incident_does_not_query_vlm_index(self):
        record = {"Id": INCIDENT_ID, "sensorId": "Camera"}
        client = FakeES([record])
        self.assertIs(await self.call_lookup(client), record)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["index_key"], "incidents")

    async def test_vlm_only_rule_incident_is_returned_unchanged(self):
        record = {
            "Id": INCIDENT_ID,
            "sensorId": "Camera_01",
            "timestamp": "2026-11-04T18:14:20.000Z",
            "end": "2026-11-04T18:14:25.000Z",
            "objectIds": [],
            "info": {"verdict": "confirmed", "description": "A forklift moves."},
        }
        client = FakeES([], [record])
        self.assertIs(await self.call_lookup(client), record)
        self.assertEqual([call["index_key"] for call in client.calls], ["incidents", "vlm_incidents"])
        self.assertEqual(record["objectIds"], [])

    async def test_fallback_is_exact_id_and_preserves_requested_fields_and_bound(self):
        client = FakeES([], [{"Id": INCIDENT_ID}])
        await self.call_lookup(client)
        for call in client.calls:
            self.assertEqual(call["query_body"], self.query)
            self.assertEqual(call["source_includes"], self.fields)
            self.assertEqual(call["size"], 1)
        self.assertEqual(self.query, {"query": {"term": {"Id.keyword": INCIDENT_ID}}})

    async def test_missing_in_both_indexes_remains_missing(self):
        client = FakeES([], [])
        self.assertEqual(await self.call_lookup(client), {})
        self.assertEqual(len(client.calls), 2)

    async def test_vlm_primary_lookup_remains_unchanged(self):
        record = {"Id": INCIDENT_ID, "sensorId": "Camera_03"}
        client = FakeES([record])
        self.assertIs(await self.call_lookup(client, vlm_verified=True), record)
        self.assertEqual([call["index_key"] for call in client.calls], ["vlm_incidents"])

    async def test_empty_vlm_primary_does_not_fall_back_to_unverified_raw(self):
        client = FakeES([])
        self.assertEqual(await self.call_lookup(client, vlm_verified=True), {})
        self.assertEqual(len(client.calls), 1)

    async def test_raw_lookup_error_is_not_hidden_as_a_miss(self):
        client = FakeES(RuntimeError("raw search failed"))
        with self.assertRaisesRegex(RuntimeError, "raw search failed"):
            await self.call_lookup(client)
        self.assertEqual(len(client.calls), 1)

    async def test_vlm_lookup_error_is_not_hidden_as_a_miss(self):
        client = FakeES([], RuntimeError("VLM search failed"))
        with self.assertRaisesRegex(RuntimeError, "VLM search failed"):
            await self.call_lookup(client)
        self.assertEqual(len(client.calls), 2)

    def test_patch_changes_only_single_incident_lookup(self):
        patch = PATCH.read_text(encoding="utf-8")
        additions = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        self.assertNotIn("_get_incidents(", additions)
        self.assertNotIn("get_incident_tool", additions)
        self.assertNotIn("http", additions)
        self.assertNotIn(".update(", additions)


if __name__ == "__main__":
    unittest.main()
