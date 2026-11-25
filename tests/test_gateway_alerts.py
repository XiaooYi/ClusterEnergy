from contextlib import closing
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from b_node.gateway.gateway import EnergyStore, now_iso


class GatewayAlertTests(unittest.TestCase):
    def test_imports_legacy_alerts_without_double_counting_or_touching_energy(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy.sqlite"
            legacy_store = EnergyStore(str(legacy))
            legacy_store.record_alerts([{"ts": now_iso(), "node": "ln4", "level": "warn",
                                         "msg": "采集序号停滞"}])
            legacy_store.close()
            original = legacy.read_bytes()

            with closing(EnergyStore(str(Path(directory) / "current.sqlite"))) as target:
                target.record_sample(datetime.fromisoformat(now_iso()), 3600, 0, 5)
                energy_before = target.get_today_kwh()
                self.assertEqual(target.import_alerts(legacy), 1)
                self.assertEqual(target.import_alerts(legacy), 0)
                report = target.query_alerts("today", "all", "all", 1, 12)
                self.assertEqual(report["total"], 1)
                self.assertEqual(report["nodes"], ["ln4"])
                self.assertEqual(target.get_today_kwh(), energy_before)
            self.assertEqual(legacy.read_bytes(), original)
