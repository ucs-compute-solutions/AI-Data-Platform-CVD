"""Public source acquisition and package navigation regressions."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = "https://github.com/ucs-compute-solutions/AI-Data-Platform-CVD"
PUBLIC_FOLDER = PUBLIC_REPOSITORY + "/tree/main/warehouse-vss-demo"


class DocumentationNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deployment = (ROOT / "deployment/DEPLOYMENT.md").read_text(encoding="utf-8")
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def acquisition_block(self):
        match = re.search(r"^```bash[ \t]*\n(.*?)^```[ \t]*$", self.deployment, re.M | re.S)
        self.assertIsNotNone(match, "Deployment guide needs a first bash acquisition block")
        return match.group(1)

    def test_deployment_links_public_folder_and_package_overview(self):
        self.assertTrue(PUBLIC_FOLDER in self.deployment, "Deployment guide must link the public Warehouse folder")
        self.assertTrue(re.search(r"\[[^\]]+\]\(\.\./README\.md(?:#[^)]+)?\)", self.deployment),
                        "Deployment guide must link back to the package README")

    def test_acquisition_commands_are_in_working_directory_order(self):
        block = re.sub(r"\\\r?\n[ \t]*", " ", self.acquisition_block())
        commands = [" ".join(line.split()) for line in block.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        expected = [
            "git clone --branch main --single-branch " + PUBLIC_REPOSITORY + ".git",
            "cd AI-Data-Platform-CVD",
            "git rev-parse HEAD",
            "python3 -B warehouse-vss-demo/verify.py",
            "cd warehouse-vss-demo/deployment",
        ]
        positions = []
        for command in expected:
            self.assertTrue(command in commands, "Acquisition command is missing or changed: " + command)
            positions.append(commands.index(command))
        self.assertEqual(positions, sorted(positions), "Acquisition commands must preserve directory and verification order")

    def test_acquisition_block_contains_no_cluster_or_execution_actions(self):
        self.assertFalse(re.search(r"\b(?:oc|apply)\b|--execute\b", self.acquisition_block()),
                         "Acquisition block must not include cluster or execution actions")

    def test_package_readme_links_public_source_and_relative_deployment(self):
        self.assertTrue(re.search(r"\[[^\]]+\]\(" + re.escape(PUBLIC_REPOSITORY)
                                 + r"(?:/tree/main/warehouse-vss-demo)?/?\)", self.readme),
                        "Package README must link the public repository or Warehouse folder")
        self.assertTrue(re.search(r"\[[^\]]+\]\(deployment/DEPLOYMENT\.md(?:#[^)]+)?\)", self.readme),
                        "Package README must retain its relative deployment guide link")


if __name__ == "__main__":
    unittest.main()
