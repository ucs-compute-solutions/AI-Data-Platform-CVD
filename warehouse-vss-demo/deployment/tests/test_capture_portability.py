"""Captured service endpoints must honor the reviewed target DNS domain."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("warehouse_capture", ROOT / "capture_to_template.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class CapturePortability(unittest.TestCase):
    def test_rtsp_and_embedded_json_service_fqdns_are_tokenized(self):
        for original in (
            "rtsp://warehouse-nvstreamer.@@NAMESPACE@@.svc.cluster.local:31554/video.mp4",
            '{"server_domain_name": "warehouse-vios.@@NAMESPACE@@.svc.cluster.local"}',
        ):
            tokenized = capture.tokenize_service_domain(original)
            self.assertNotIn(".svc.cluster.local", tokenized)
            self.assertIn(".svc.@@CLUSTER_DOMAIN@@", tokenized)

    def test_unrelated_host_and_nondefault_suffix_not_partially_rewritten(self):
        for original in ("https://cluster.local/path", "host.svc.cluster.local.example", "host.svc.custom.example"):
            self.assertEqual(capture.tokenize_service_domain(original), original)


if __name__ == "__main__":
    unittest.main()
