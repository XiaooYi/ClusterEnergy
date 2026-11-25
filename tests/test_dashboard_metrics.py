from contextlib import closing
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from b_node.gateway.gateway import EnergyStore, GatewayState, TZ_CST
from common.storage import SampleStore


class DashboardMetricsTests(TestCase):
    def test_cards_return_week_energy_coverage_and_omit_future_minutes(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=TZ_CST)
        with closing(EnergyStore(":memory:")) as store, patch("b_node.gateway.gateway.local_now", return_value=now):
            for day in (7, 6, 0):
                store.record_sample(now - timedelta(days=day, minutes=2), 3600, 0, 10)
            store.record_sample(now + timedelta(minutes=2), 3600, 0, 10)

            self.assertEqual(store.card_energy(), {"today_kwh": 0.01, "energy_7d_kwh": 0.02, "energy_7d_days": 2})
            stats = store.query_stats("today")
            self.assertEqual(stats["date"], "2026-11-25")
            self.assertEqual([point["ts"] for point in stats["points"]], ["2026-11-25T11:58"])

    def test_corrects_simulated_upstream_timestamps_but_keeps_watt_values(self):
        with closing(EnergyStore(":memory:")) as store, SampleStore(":memory:") as archive:
            gateway = GatewayState("https://example", None, store, archive, 5, 30, 30, "simulated", None,
                                   sim_clock_offset_seconds=-650)
            gateway.next_cleanup = float("inf")
            start = "2026-11-25T12:08:50.500Z"
            responses = {
                "/api/snapshot": {"mode": "simulated", "updated_at": start, "sampled_at": start,
                                  "cluster": {"total_power": 500}, "nodes": []},
                "/api/history": {"updated_at": start, "points": [{"ts": start, "cluster_total": 500}]},
                "/api/samples": {"samples": [{"collected_at": start, "node_id": "sim-cn001", "watts": 500}],
                                 "next_cursor": 1, "stream_id": "test-stream", "history_gap": False},
            }
            def response(_address, _ctx, route, _params=None):
                return responses[route]

            with patch("b_node.gateway.gateway.get_json", side_effect=response), \
                    patch("common.transport.get_json", side_effect=response):
                gateway.poll_once()

            corrected = "2026-11-25T11:58:00.500Z"
            self.assertEqual(gateway.source_time(), corrected)
            self.assertEqual(gateway.history["points"], [{"ts": corrected, "cluster_total": 500}])
            self.assertEqual(archive.latest(), [{"collected_at": corrected, "node_id": "sim-cn001", "watts": 500}])
