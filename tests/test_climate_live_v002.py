import unittest
from unittest.mock import patch

from polymarket_bot.climate_live_v002 import (
    _event_fee_schedule,
    fetch_active_temperature_events,
    operational_contract_v2,
)


class ClimateLiveV2Tests(unittest.TestCase):
    def test_noaa_daily_contract_with_known_station_is_operational(self) -> None:
        event = {
            "description": (
                'Highest temperature recorded by NOAA. The highest reading under the "Temp" column '
                "for all times on this day: https://www.weather.gov/wrh/timeseries?site=klga"
            )
        }
        contract = {
            "rules_complete": True,
            "station_id": "KLGA",
            "unit": "°F",
            "precision": 1.0,
            "source_type": "NOAA_NWS",
        }
        station = {"metadata_complete": "True", "latitude": "40.7", "longitude": "-73.8"}
        ok, reason = operational_contract_v2(event, contract, station, {("KLGA", "°F", 1.0)})
        self.assertTrue(ok, reason)

    def test_changed_station_unit_precision_is_blocked(self) -> None:
        event = {"description": 'under the "Temp" column for all times on this day site=xxxx'}
        contract = {
            "rules_complete": True,
            "station_id": "XXXX",
            "unit": "°C",
            "precision": 0.1,
            "source_type": "NOAA_NWS",
        }
        station = {"metadata_complete": "True", "latitude": "1", "longitude": "2"}
        ok, _ = operational_contract_v2(event, contract, station, set())
        self.assertFalse(ok)

    def test_wunderground_daily_table_contract_is_operational(self) -> None:
        event = {
            "description": (
                "Use the Daily Observations table and the highest temperature recorded for all times on this day. "
                "https://www.wunderground.com/history/daily/tw/taipei/RCSS"
            )
        }
        contract = {
            "rules_complete": True,
            "station_id": "RCSS",
            "unit": "°C",
            "precision": 1.0,
            "source_type": "WEATHER_UNDERGROUND",
            "resolution_source": "https://www.wunderground.com/history/daily/tw/taipei/RCSS",
            "first_next_day_required": True,
        }
        station = {"metadata_complete": "True", "latitude": "25", "longitude": "121"}
        ok, reason = operational_contract_v2(event, contract, station, {("RCSS", "°C", 1.0)})
        self.assertTrue(ok, reason)

    def test_fee_schedule_must_be_identical_and_enabled(self) -> None:
        event = {"markets": [
            {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}},
            {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}},
        ]}
        self.assertEqual(_event_fee_schedule(event)["rate"], 0.05)

    @patch("polymarket_bot.climate_live_v002.http_json")
    def test_active_discovery_paginates_until_short_page(self, request) -> None:
        request.side_effect = [
            [{"id": str(index)} for index in range(100)],
            [{"id": "100"}],
        ]
        result = fetch_active_temperature_events()
        self.assertEqual(len(result), 101)
        self.assertEqual(request.call_count, 2)
