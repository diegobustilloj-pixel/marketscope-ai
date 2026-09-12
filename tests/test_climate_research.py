from polymarket_bot.climate_research import infer_market_date, parse_bucket, parse_resolution_contract


def test_parse_celsius_point_bucket() -> None:
    assert parse_bucket("26°C") == (26.0, 26.0, "°C")


def test_parse_fahrenheit_range_bucket() -> None:
    assert parse_bucket("72-73°F") == (72.0, 73.0, "°F")


def test_parse_open_buckets() -> None:
    assert parse_bucket("17°C or below") == (None, 17.0, "°C")
    assert parse_bucket("27°C or higher") == (27.0, None, "°C")


def test_resolution_contract_extracts_station_and_precision() -> None:
    event = {
        "id": "1",
        "slug": "highest-temperature-in-mexico-city-on-september-3-2026",
        "title": "Highest temperature in Mexico City on September 3?",
        "description": (
            "This market will resolve to the temperature range that contains the highest temperature "
            "recorded by NOAA at the Benito Juárez International Airport Station in degrees Celsius. "
            "See https://www.weather.gov/wrh/timeseries?site=mmmx. The resolution source for this "
            "market measures temperatures to whole degrees Celsius."
        ),
        "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=mmmx",
        "markets": [
            {
                "id": "m1",
                "groupItemTitle": "20°C or below",
                "clobTokenIds": '["yes","no"]',
                "outcomePrices": '["0","1"]',
            },
            {
                "id": "m2",
                "groupItemTitle": "21°C or higher",
                "clobTokenIds": '["yes2","no2"]',
                "outcomePrices": '["1","0"]',
            },
        ],
    }
    contract = parse_resolution_contract(event)
    assert contract["station_id"] == "MMMX"
    assert contract["icao"] == "MMMX"
    assert contract["unit"] == "°C"
    assert contract["precision"] == 1.0
    assert contract["source_type"] == "NOAA_NWS"


def test_infer_market_date_uses_contract_day_not_utc_close() -> None:
    assert infer_market_date(
        "Highest temperature in Hong Kong on March 18?", "2026-03-19T12:00:00Z"
    ) == "2026-03-18"
    assert infer_market_date(
        "Highest temperature in NYC on December 30?", "2025-12-30T12:00:00Z"
    ) == "2025-12-30"
