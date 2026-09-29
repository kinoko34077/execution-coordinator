from __future__ import annotations

import inspect
import unittest

from execution_coordinator.bootstrap_pickup import main


class Stage1WorkClassCliTests(unittest.TestCase):
    def test_pickup_exposes_repeatable_work_class_constraint(self):
        source = inspect.getsource(main)
        self.assertIn('pick.add_argument("--work-class"', source)
        self.assertIn("accepted_work_classes=", source)


if __name__ == "__main__":
    unittest.main()
