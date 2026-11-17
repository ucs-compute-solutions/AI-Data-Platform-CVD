"""Browser-only package tests; no server, browser or third-party module needed."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import build_browser as browser


class BrowserBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="browser-demo-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        capture = self.root / "capture"
        self.report_text = '# Exact report\r\n<script> & quoted "text"\r\n\u2028\u2029 café\r\n'
        for name, content in {"screen.jpg": b"\xff\xd8\xfffixture-jpeg",
                              "clip.mp4": b"\0\0\0\x18ftypmp42fixture-video",
                              "report.pdf": b"%PDF-1.4\nfixture",
                              "report.md": self.report_text.encode("utf-8")}.items():
            path = capture / "assets" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        extras = []
        for number in range(55):
            name = f"assets/extra-{number:02}.jpg"
            (capture / name).write_bytes(b"\xff\xd8\xffextra-image")
            extras.append({"src": name})
        scenario = {"id": "hard-hat-missing", "title": "Hard Hat Missing", "camera": "Camera_01",
                    "rule": {"name": "Hard Hat Missing", "prompt": "Is a hard hat missing?"},
                    "incident": {"id": "fixture-incident", "startUtc": "2026-11-15T18:00:00Z",
                                 "endUtc": "2026-11-15T18:00:30Z"},
                    "steps": [{"id": name, "title": name, "instruction": "Select the highlighted control.",
                               "image": "assets/screen.jpg"}
                              for name in ("createRule", "activeRule", "alert", "play", "report", "followup")],
                    "video": {"src": "assets/clip.mp4"},
                    "reports": [{"type": "pdf", "label": "PDF", "src": "assets/report.pdf",
                                 "pages": [{"src": "assets/screen.jpg", "page": number,
                                            "width": 100, "height": 150} for number in (1, 2, 3)]},
                                {"type": "markdown", "label": "Markdown", "src": "assets/report.md"}],
                    "followups": [{"question": "What is visible?", "answer": "Recorded fixture answer.",
                                   "source": {"kind": "captured-chat", "label": "Recorded fixture",
                                              "src": "assets/screen.jpg"}}]}
        second = copy.deepcopy(scenario)
        second.update(id="forklift-and-person", title="Forklift and Person", camera="Camera")
        self.manifest = {"schemaVersion": 1, "title": "Cisco AI POD demo", "captureDate": "2026-11-15",
                         "overview": {"image": "assets/screen.jpg", "video": "assets/clip.mp4",
                                      "captureAssets": extras}, "scenarios": [scenario, second]}
        manifest_path = capture / "demo.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.source = self.root / "verified-source"
        browser.load_verifier().load_builder().build(manifest_path, capture, self.source)
        self.source_before = browser.records(self.source)
        self.shared_app = self.root / "shared-app"
        self.shared_app.mkdir()
        for name in ("core.js", "style.css"):
            shutil.copyfile(self.source / name, self.shared_app / name)
        (self.shared_app / "app.js").write_text(
            '/* Reviewed fixture: window.WarehouseOfflineCapture */\n', encoding="utf-8")
        self.app_patch = mock.patch.object(browser, "APP_ROOT", self.shared_app)
        self.app_patch.start()
        self.addCleanup(self.app_patch.stop)
        self.output = self.root / "browser-release"

    def build(self):
        return browser.build(self.source, self.output)

    def rewrite_manifest(self, update):
        data = json.loads((self.source / "demo.json").read_text())
        update(data)
        (self.source / "demo.json").write_text(json.dumps(data), encoding="utf-8")
        summary_path = self.source / "checksums.json"
        summary = json.loads(summary_path.read_text())
        for item in summary["files"]:
            path = self.source / item["path"]
            item.update(bytes=path.stat().st_size, sha256=browser.digest(path))
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

    @staticmethod
    def decode_payload(script):
        prefix = "window.WarehouseOfflineCapture = "
        assert script.startswith(prefix) and script.endswith(";\n")
        return json.loads(script[len(prefix):-2])

    def test_all_59_assets_movie_and_source_preserved(self):
        result = self.build()
        release = self.output / browser.RELEASE_NAME
        expected = browser.load_verifier().load_builder().validate_manifest(self.manifest)
        self.assertEqual(len(expected), 59)
        self.assertEqual(result["source"]["assets"], 59)
        for name in expected | {"demo.json", "core.js", "style.css"}:
            self.assertEqual(browser.digest(self.source / name), browser.digest(release / name))
        self.assertEqual(browser.digest(self.shared_app / "app.js"), browser.digest(release / "app.js"))
        self.assertEqual(self.source_before, browser.records(self.source))
        self.assertEqual(result["source"]["presentationMovie"]["sha256"], browser.digest(release / "assets/clip.mp4"))
        for key in ("recipientPythonRequired", "recipientServerRequired", "recipientExecutableRequired"):
            self.assertFalse(result[key])

    def test_exact_manifest_and_markdown_offline_payload(self):
        self.build()
        script = browser.exact_text(self.output / browser.RELEASE_NAME / "offline-data.js")
        data = self.decode_payload(script)
        self.assertEqual(data, {"format": 1, "manifest": self.manifest,
                                "reports": {"assets/report.md": self.report_text}})
        self.assertIn("\r\n", data["reports"]["assets/report.md"])

    def test_javascript_serialization_escapes_markup_and_unicode_separators(self):
        malicious = '</script><script>alert("x")</script>&\u2028\u2029'
        content, _ = browser.offline_script({"note": malicious}, {"assets/report.md": malicious})
        text = content.decode("utf-8")
        for character in ("<", ">", "&", "\u2028", "\u2029"):
            self.assertNotIn(character, text)
        for escaped in ("\\u003c", "\\u003e", "\\u0026", "\\u2028", "\\u2029"):
            self.assertIn(escaped, text)
        self.assertEqual(self.decode_payload(text)["manifest"]["note"], malicious)

    def test_index_alias_classic_script_order_home_and_offline_csp(self):
        self.build()
        release = self.output / browser.RELEASE_NAME
        text = browser.exact_text(release / "Start Demo.html")
        self.assertEqual((release / "Start Demo.html").read_bytes(), (release / "index.html").read_bytes())
        self.assertLess(text.index('<script src="core.js"'), text.index('<script src="offline-data.js"'))
        self.assertLess(text.index('<script src="offline-data.js"'), text.index('<script src="app.js"'))
        self.assertIn('<script src="offline-data.js" defer></script>', text)
        self.assertNotIn('type="module"', text)
        self.assertNotIn('class="brand" href="./"', text)
        self.assertIn('class="brand" href="Start%20Demo.html"', text)
        self.assertIn(browser.OFFLINE_CSP, text)
        self.assertIn("connect-src 'none'", text)
        self.assertIn("script-src 'self' file:", text)
        self.assertNotIn("'unsafe-inline'", text)
        self.assertNotIn("'unsafe-eval'", text)
        self.assertNotIn("https:", browser.OFFLINE_CSP)
        self.assertNotIn("http:", browser.OFFLINE_CSP)
        self.assertNotIn(browser.OFFLINE_CSP, browser.exact_text(self.source / "index.html"))

    def test_no_recipient_runtime_or_authoring_tools(self):
        self.build()
        release = self.output / browser.RELEASE_NAME
        for path in release.rglob("*"):
            self.assertNotIn(path.suffix, {".py", ".pyc", ".exe", ".app", ".command", ".sh"})
            self.assertNotEqual(path.name, "tools")
        readme = browser.exact_text(release / "README.txt")
        self.assertIn("Double-click Start Demo.html", readme)
        self.assertIn("No installation, Python", readme)
        self.assertNotIn("127.0.0.1", readme)
        self.assertNotIn("http://localhost", readme)
        self.assertIn("no server to stop", readme)

    def test_release_reports_hashes_and_safe_zip(self):
        result = self.build()
        release = self.output / browser.RELEASE_NAME
        report_text = browser.exact_text(release / "BUILD-REPORT.json")
        self.assertNotIn(str(self.root), report_text)
        report = json.loads(report_text)
        self.assertEqual(len(report["assets"]), 59)
        self.assertEqual(report["offlineDataFileSha256"], browser.digest(release / "offline-data.js"))
        self.assertEqual(report["markdownReports"], 1)
        self.assertEqual(report["source"]["checksumsSha256"], browser.digest(self.source / "checksums.json"))
        for line in (release / "SHA256SUMS.txt").read_text().splitlines():
            checksum, name = line.split("  ", 1)
            self.assertNotEqual(name, "SHA256SUMS.txt")
            self.assertEqual(checksum, browser.digest(release / name))
        with zipfile.ZipFile(self.output / result["archive"]) as bundle:
            self.assertIsNone(bundle.testzip())
            for name in bundle.namelist():
                self.assertTrue(name.startswith(browser.RELEASE_NAME + "/"))
                self.assertFalse(name.startswith("/"))
                self.assertNotIn("..", Path(name).parts)
            self.assertIn(browser.RELEASE_NAME + "/Start Demo.html", bundle.namelist())
        self.assertEqual(result["archiveSha256"], browser.digest(self.output / result["archive"]))
        self.assertEqual(result["fileBrowserAcceptance"], "pending")
        self.assertEqual(report["fileBrowserAcceptance"], "pending")

    def test_existing_output_and_source_nested_output_rejected(self):
        self.output.mkdir()
        marker = self.output / "retain.txt"
        marker.write_text("retained", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build()
        self.assertEqual(marker.read_text(), "retained")
        with self.assertRaisesRegex(ValueError, "outside"):
            browser.build(self.source, self.source / "nested")
        self.assertEqual(self.source_before, browser.records(self.source))

    def test_corrupt_source_and_extra_files_rejected_before_output(self):
        with (self.source / "assets/clip.mp4").open("ab") as stream:
            stream.write(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Checksum"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_malicious_asset_path_rejected_even_with_refreshed_checksum(self):
        self.rewrite_manifest(lambda manifest: manifest["scenarios"][0]["video"].update(src="assets/../outside.mp4"))
        with self.assertRaisesRegex(ValueError, "Unsafe|unsupported"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_remote_asset_path_rejected(self):
        self.rewrite_manifest(lambda manifest: manifest["scenarios"][0]["video"].update(src="https://example.test/video.mp4"))
        with self.assertRaisesRegex(ValueError, "Unsafe|unsupported"):
            self.build()

    def test_all_pdf_reports_need_local_page_images(self):
        self.rewrite_manifest(lambda manifest: manifest["scenarios"][0]["reports"][0].pop("pages"))
        with self.assertRaisesRegex(ValueError, "Every PDF"):
            self.build()

    def test_shared_core_or_style_change_requires_review(self):
        (self.shared_app / "style.css").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "style.css differs"):
            self.build()

    def test_offline_loader_must_be_present(self):
        (self.shared_app / "app.js").write_text("/* no data loader */", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "offline data loader"):
            self.build()

    def test_html_must_have_one_classic_app_entry_and_brand_home(self):
        for text in ('<a class="brand" href="./"></a>',
                     '<script src="app.js" defer></script>',
                     '<a class="brand" href="./"></a><script src="app.js" defer></script>' * 2,
                     '<a class="brand" href="./"></a><script type="module" src="app.js" defer></script>'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    browser.browser_index(text)


if __name__ == "__main__":
    unittest.main()
