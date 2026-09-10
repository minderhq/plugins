"""Behavioural tests for the weather plugin — location-spec parsing, config
coercion, the geocode + current-conditions fetch/parse (incl. their fail-soft
None paths), and the get_weather / collect_data entry points. HTTP is faked.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

import weather
from weather import WeatherPlugin


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _json_client(payload, *, raise_on_get=False):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if raise_on_get:
                raise RuntimeError("boom")
            return _FakeResp(payload)

    return _FakeClient


def _post_client(*, raise_on_post=False):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None, headers=None, content=None):
            if raise_on_post:
                raise RuntimeError("boom")
            return _FakeResp(None)

    return _FakeClient


# ── _parse_locations ─────────────────────────────────────────────────────────
def test_parse_locations_triples():
    out = WeatherPlugin._parse_locations("Istanbul:41.0:28.9, Ankara:39.9:32.8")
    assert out == [("Istanbul", 41.0, 28.9), ("Ankara", 39.9, 32.8)]


def test_parse_locations_skips_wrong_arity_and_non_float():
    # missing a coord (2 parts), extra colon (4 parts), non-numeric coord — all dropped
    assert WeatherPlugin._parse_locations(
        "Bad:1.0, X:1:2:3, NaN:abc:2.0, Ok:1.5:2.5"
    ) == [("Ok", 1.5, 2.5)]


def test_parse_locations_empty():
    assert WeatherPlugin._parse_locations("") == []


# ── apply_config coercion ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [(True, True), (False, False), ("1", True), ("on", True), ("no", False)],
)
def test_apply_config_sink_bool_coercion(value, expected):
    p = WeatherPlugin()
    p.apply_config({"WEATHER_SINK_INFLUXDB": value})
    assert p.sink_influxdb is expected


def test_apply_config_maps_all_keys_at_once():
    p = WeatherPlugin()
    p.apply_config(
        {"WEATHER_LOCATIONS": "Tokyo:35.68:139.69", "WEATHER_SINK_INFLUXDB": False}
    )
    assert p.locations == [("Tokyo", 35.68, 139.69)]
    assert p.sink_influxdb is False


def test_init_default_locations_bootstrap():
    p = WeatherPlugin()
    assert p.locations == [("Istanbul", 41.01, 28.98), ("London", 51.51, -0.13)]
    assert p.sink_influxdb is True


# ── lifecycle ────────────────────────────────────────────────────────────────
def test_register_and_health():
    p = WeatherPlugin()
    md = asyncio.run(p.register())
    assert md.name == "weather"
    h = asyncio.run(p.health_check())
    assert h["healthy"] is True
    assert set(h["locations"]) == {n for n, _, _ in p.locations}
    assert h["influxdb_sink"] is p.sink_influxdb


def test_lifecycle_status_transitions():
    p = WeatherPlugin()
    assert p.status == "registered"
    asyncio.run(p.initialize())
    assert p.status == "ready"
    asyncio.run(p.shutdown())
    assert p.status == "shutdown"


# ── _fetch_current ───────────────────────────────────────────────────────────
def test_fetch_current_maps_api_fields(monkeypatch):
    p = WeatherPlugin()
    payload = {
        "current": {
            "temperature_2m": 21.4,
            "relative_humidity_2m": 55,
            "wind_speed_10m": 3.2,
        }
    }
    monkeypatch.setattr(weather.httpx, "AsyncClient", _json_client(payload))
    r = asyncio.run(p._fetch_current(1.0, 2.0))
    assert r == {"temperature": 21.4, "humidity": 55, "wind_speed": 3.2}


def test_fetch_current_missing_current_key_yields_nones(monkeypatch):
    p = WeatherPlugin()
    monkeypatch.setattr(weather.httpx, "AsyncClient", _json_client({}))
    assert asyncio.run(p._fetch_current(1.0, 2.0)) == {
        "temperature": None,
        "humidity": None,
        "wind_speed": None,
    }


def test_fetch_current_none_on_error(monkeypatch):
    p = WeatherPlugin()
    monkeypatch.setattr(
        weather.httpx, "AsyncClient", _json_client({}, raise_on_get=True)
    )
    assert asyncio.run(p._fetch_current(1.0, 2.0)) is None


# ── _geocode fail-soft paths ─────────────────────────────────────────────────
def test_geocode_resolves_first_result(monkeypatch):
    p = WeatherPlugin()
    payload = {"results": [{"latitude": "41.0", "longitude": "28.9"}]}
    monkeypatch.setattr(weather.httpx, "AsyncClient", _json_client(payload))
    assert asyncio.run(p._geocode("Istanbul")) == (41.0, 28.9)


@pytest.mark.parametrize(
    "payload",
    [
        {"results": []},  # no match
        {"results": ["not-a-dict"]},  # unexpected shape
        {"results": [{"latitude": 1.0}]},  # missing lon
        {"results": [{"latitude": "x", "longitude": "y"}]},  # non-numeric coords
        {},  # missing results key entirely
    ],
)
def test_geocode_returns_none_on_bad_shapes(monkeypatch, payload):
    p = WeatherPlugin()
    monkeypatch.setattr(weather.httpx, "AsyncClient", _json_client(payload))
    assert asyncio.run(p._geocode("wherever")) is None


def test_geocode_none_on_error(monkeypatch):
    p = WeatherPlugin()
    monkeypatch.setattr(
        weather.httpx, "AsyncClient", _json_client({}, raise_on_get=True)
    )
    assert asyncio.run(p._geocode("x")) is None


# ── get_weather action ───────────────────────────────────────────────────────
def _stub(plugin, monkeypatch, *, coords, reading):
    async def _geo(_name):
        return coords

    async def _cur(_lat, _lon):
        return reading

    monkeypatch.setattr(plugin, "_geocode", _geo)
    monkeypatch.setattr(plugin, "_fetch_current", _cur)


def test_get_weather_requires_location():
    assert asyncio.run(WeatherPlugin().get_weather("")) == {
        "error": "location is required"
    }


def test_get_weather_unresolvable_location(monkeypatch):
    p = WeatherPlugin()
    _stub(p, monkeypatch, coords=None, reading=None)
    assert asyncio.run(p.get_weather("Nowhere")) == {
        "location": "Nowhere",
        "error": "could not resolve location",
    }


def test_get_weather_unavailable_reading(monkeypatch):
    p = WeatherPlugin()
    _stub(p, monkeypatch, coords=(1.0, 2.0), reading=None)
    assert asyncio.run(p.get_weather("City")) == {
        "location": "City",
        "error": "weather unavailable",
    }


def test_get_weather_success_merges_reading(monkeypatch):
    p = WeatherPlugin()
    _stub(
        p,
        monkeypatch,
        coords=(1.0, 2.0),
        reading={"temperature": 10, "humidity": 40, "wind_speed": 1},
    )
    assert asyncio.run(p.get_weather("City")) == {
        "location": "City",
        "temperature": 10,
        "humidity": 40,
        "wind_speed": 1,
    }


# ── collect_data ─────────────────────────────────────────────────────────────
def test_collect_data_skips_failed_locations_and_gates_influx(monkeypatch):
    p = WeatherPlugin()
    p.locations = [("A", 1.0, 2.0), ("B", 3.0, 4.0)]
    p.sink_influxdb = False

    async def _cur(lat, _lon):
        # location B (lat 3.0) fails to fetch → must be skipped, not stored
        return (
            None if lat == 3.0 else {"temperature": 5, "humidity": 1, "wind_speed": 1}
        )

    monkeypatch.setattr(p, "_fetch_current", _cur)
    res = asyncio.run(p.collect_data())
    assert set(res["readings"]) == {"A"}
    assert res["influxdb_written"] is False


def test_write_influxdb_emits_float_fields_not_integers(monkeypatch):
    """Guard the type-preservation decision: this series has always been float
    (the old writer emitted a bare value, no 'i' suffix). An integer humidity
    (2) must still be written as a FLOAT (2.0), or InfluxDB rejects the point on
    a field-type conflict with the existing float column."""
    p = WeatherPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    cap = {}

    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None, headers=None, content=None):
            cap["content"] = content
            return _Resp()

    monkeypatch.setattr(weather.httpx, "AsyncClient", _Client)
    ok = asyncio.run(
        p._write_influxdb(
            {"Ankara": {"temperature": 5.0, "humidity": 2, "wind_speed": 3}}
        )
    )
    assert ok is True
    # no 'i' suffix anywhere — every field is a float
    assert (
        cap["content"]
        == "weather,location=Ankara temperature=5.0,humidity=2.0,wind_speed=3.0"
    )
    assert "i," not in cap["content"] and not cap["content"].rstrip().endswith("i")


# ── _write_influxdb gating / fail-soft ───────────────────────────────────────
_READING = {"Istanbul": {"temperature": 20, "humidity": 50, "wind_speed": 5}}


def test_write_influxdb_noop_when_sink_disabled():
    p = WeatherPlugin()
    p.sink_influxdb = False
    p.config = {"influxdb": {"enabled": True}}
    assert asyncio.run(p._write_influxdb(_READING)) is False


def test_write_influxdb_noop_when_influxdb_not_enabled():
    p = WeatherPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": False}}
    assert asyncio.run(p._write_influxdb(_READING)) is False


def test_write_influxdb_noop_when_readings_empty():
    p = WeatherPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    assert asyncio.run(p._write_influxdb({})) is False


def test_write_influxdb_noop_when_no_numeric_fields():
    # all-None readings produce no line-protocol lines → nothing to write.
    p = WeatherPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    readings = {"Istanbul": {"temperature": None, "humidity": None, "wind_speed": None}}
    assert asyncio.run(p._write_influxdb(readings)) is False


def test_write_influxdb_failure_returns_false(monkeypatch):
    p = WeatherPlugin()
    p.sink_influxdb = True
    p.config = {"influxdb": {"enabled": True}}
    monkeypatch.setattr(weather.httpx, "AsyncClient", _post_client(raise_on_post=True))
    assert asyncio.run(p._write_influxdb(_READING)) is False


# ── collect_data aggregation / analyze / refresh ─────────────────────────────
def test_collect_data_aggregates_across_locations(monkeypatch):
    p = WeatherPlugin()
    p.locations = [("A", 1.0, 1.0), ("B", 2.0, 2.0)]

    async def fake_fetch(lat, lon):
        return {"temperature": lat, "humidity": 50, "wind_speed": 5}

    monkeypatch.setattr(p, "_fetch_current", fake_fetch)
    monkeypatch.setattr(p, "_write_influxdb", AsyncMock(return_value=False))
    result = asyncio.run(p.collect_data())
    assert result["readings"] == {
        "A": {"temperature": 1.0, "humidity": 50, "wind_speed": 5},
        "B": {"temperature": 2.0, "humidity": 50, "wind_speed": 5},
    }
    assert result["influxdb_written"] is False
    assert p._last == result


def test_collect_data_calls_write_influxdb_with_readings(monkeypatch):
    p = WeatherPlugin()
    p.locations = [("A", 1.0, 1.0)]
    monkeypatch.setattr(
        p,
        "_fetch_current",
        AsyncMock(return_value={"temperature": 1, "humidity": 1, "wind_speed": 1}),
    )
    write_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(p, "_write_influxdb", write_mock)
    result = asyncio.run(p.collect_data())
    write_mock.assert_awaited_once_with(result["readings"])
    assert result["influxdb_written"] is True


def test_analyze_before_any_collection():
    p = WeatherPlugin()
    out = asyncio.run(p.analyze())
    assert out["message"] == "no data collected yet"
    assert set(out["locations"]) == {n for n, _, _ in p.locations}


def test_analyze_returns_last_collection(monkeypatch):
    p = WeatherPlugin()
    p.locations = [("A", 1.0, 1.0)]
    monkeypatch.setattr(
        p,
        "_fetch_current",
        AsyncMock(return_value={"temperature": 1, "humidity": 1, "wind_speed": 1}),
    )
    monkeypatch.setattr(p, "_write_influxdb", AsyncMock(return_value=False))
    asyncio.run(p.collect_data())
    assert asyncio.run(p.analyze()) == p._last


def test_refresh_calls_collect_data(monkeypatch):
    p = WeatherPlugin()
    monkeypatch.setattr(p, "collect_data", AsyncMock(return_value={"ok": True}))
    assert asyncio.run(p.refresh()) == {"ok": True}


def test_get_weather_geocode_lat_none_returns_error(monkeypatch):
    # _geocode can return a (None, lon) tuple; the `coords[0] is None` guard must
    # still treat it as unresolved rather than fetching with a None latitude.
    p = WeatherPlugin()
    monkeypatch.setattr(p, "_geocode", AsyncMock(return_value=(None, 1.0)))
    out = asyncio.run(p.get_weather("Nowhere"))
    assert out == {"location": "Nowhere", "error": "could not resolve location"}
