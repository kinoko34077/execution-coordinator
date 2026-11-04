from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageDistributionTests(unittest.TestCase):
    def test_distribution_packages_existing_source_without_runtime_dependencies(self):
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("kinotch-execution-coordinator", manifest["project"]["name"])
        self.assertEqual(">=3.11", manifest["project"]["requires-python"])
        self.assertEqual([], manifest["project"].get("dependencies", []))
        self.assertEqual("setuptools.build_meta", manifest["build-system"]["build-backend"])

        package_finder = manifest["tool"]["setuptools"]["packages"]["find"]
        self.assertEqual(["src"], package_finder["where"])
        self.assertEqual(["execution_coordinator*"], package_finder["include"])
