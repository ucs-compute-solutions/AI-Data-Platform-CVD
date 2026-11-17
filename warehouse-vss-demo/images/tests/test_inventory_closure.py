"""Every selected runtime, initialization and population image must be preserved."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment"))
import populate_assets


def image_pins(value):
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get("image"), str):
            found.add(value["image"])
        for child in value.values():
            found.update(image_pins(child))
    elif isinstance(value, list):
        for child in value:
            found.update(image_pins(child))
    return found


class InventoryClosureTests(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads((ROOT / "deployment/baseline/baseline.captured.json").read_text())
        self.inventory = json.loads((ROOT / "images/required-images.json").read_text())

    def test_runtime_initialization_and_population_closure_is_exact(self):
        selected = image_pins(self.baseline["resources"]) | image_pins(self.baseline["bootstrap"]["jobs"])
        selected.add(populate_assets.HELPER_IMAGE)
        self.assertEqual(selected, {item["packagePin"] for item in self.inventory["images"]})
        self.assertEqual(len(selected), 25)

    def test_baseline_catalog_has_no_missing_or_extra_runtime_pins(self):
        captured = set(json.loads((ROOT / "deployment/baseline/images.json").read_text()))
        self.assertEqual(captured, image_pins(self.baseline["resources"]) | image_pins(self.baseline["bootstrap"]["jobs"]))

    def test_source_index_and_platform_relations_are_explicit(self):
        self.assertEqual(self.inventory["platform"], "linux/amd64")
        for item in self.inventory["images"]:
            original = item["packagePin"].rsplit("@", 1)[1]
            self.assertEqual(original, item["sourceIndexDigest"] or item["platformDigest"])


if __name__ == "__main__":
    unittest.main()
