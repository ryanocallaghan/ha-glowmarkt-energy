"""Tests for the config flow, options flow and statistics import."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.glowmarkt.api import GlowmarktAuthError
from custom_components.glowmarkt.const import DOMAIN
from homeassistant import config_entries
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

CLIENT = "custom_components.glowmarkt.api.GlowmarktClient"
RESOURCES = [
    {"classifier": "electricity.consumption", "resourceId": "elec"},
    {"classifier": "electricity.consumption.cost", "resourceId": "elec-cost"},
]
LOGIN = {"username": "me@example.com", "password": "pw"}
CHARGES = {
    "electricity_unit_rate": 25.0,
    "electricity_standing_charge": 60.0,
    "electricity_first_rate": 0,
    "electricity_first_rate_kwh": 0,
    "gas_unit_rate": 0,
    "gas_standing_charge": 0,
    "gas_first_rate": 0,
    "gas_first_rate_kwh": 0,
}
# Utilita Smart Energy electricity, from a real tariff page.
SMART_ENERGY = {
    **CHARGES,
    "electricity_unit_rate": 25.78,
    "electricity_standing_charge": 0,
    "electricity_first_rate": 47.23,
    "electricity_first_rate_kwh": 2,
}
NOW = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
LAST_HOUR = NOW.replace(minute=0)
# Imports start at local (BST) midnight: 60 and 3 days back respectively.
BACKFILL_START = datetime(2026, 8, 20, 23, tzinfo=UTC)
REFRESH_START = datetime(2026, 10, 16, 23, tzinfo=UTC)


def _hours(start: datetime) -> int:
    return int((LAST_HOUR - start) / timedelta(hours=1)) + 1


class FakeMeter:
    """Return a fixed kWh value for every hour asked for."""

    def __init__(self, kwh: float) -> None:
        self.kwh = kwh

    async def readings(self, resource_id, start, end, period="PT1H"):
        hour = start.replace(minute=0, second=0, microsecond=0)
        out = []
        while hour <= end:
            out.append((hour, self.kwh))
            hour += timedelta(hours=1)
        return out


async def _sums(hass: HomeAssistant, statistic_id: str) -> list[tuple[datetime, float, float]]:
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        NOW - timedelta(days=90),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum", "state"},
    )
    return [
        (datetime.fromtimestamp(r["start"], UTC), r["state"], r["sum"])
        for r in rows.get(statistic_id, [])
    ]


async def test_config_flow_creates_entry_with_charges(hass: HomeAssistant) -> None:
    """Login, then charges, gives one entry with the charges as options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(f"{CLIENT}.resources", return_value=RESOURCES):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "charges"

    with patch("custom_components.glowmarkt.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CHARGES)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == LOGIN
    assert result["options"] == CHARGES


async def test_config_flow_bad_password(hass: HomeAssistant) -> None:
    """A rejected login shows an error on the same form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(f"{CLIENT}.resources", side_effect=GlowmarktAuthError):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_opens_and_saves(hass: HomeAssistant) -> None:
    """The Configure dialog loads (the old integration's 500) and saves."""
    entry = MockConfigEntry(domain=DOMAIN, data=LOGIN, options=CHARGES)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    new = {**CHARGES, "electricity_unit_rate": 27.5}
    with patch("custom_components.glowmarkt.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], new
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == new


async def test_imports_usage_and_cost_statistics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Backfill, then a later update re-imports the trailing window."""
    freezer.move_to(NOW)
    await hass.config.async_update(currency="GBP", time_zone="Europe/London")
    entry = MockConfigEntry(domain=DOMAIN, data=LOGIN, options=CHARGES)
    entry.add_to_hass(hass)
    meter = FakeMeter(1.0)
    with (
        patch(f"{CLIENT}.resources", return_value=RESOURCES),
        patch(f"{CLIENT}.catchup", return_value=None),
        patch(f"{CLIENT}.readings", side_effect=meter.readings),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        usage = await _sums(hass, "glowmarkt:electricity_consumption")
        cost = await _sums(hass, "glowmarkt:electricity_cost")
        hours = _hours(BACKFILL_START)
        assert len(usage) == hours
        assert usage[-1][2] == pytest.approx(hours * 1.0)
        assert cost[-1][2] == pytest.approx(hours * (0.25 + 0.60 / 24))

        # Late data: the last 3 days now read 2 kWh/h. Totals before stay put.
        meter.kwh = 2.0
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        usage = await _sums(hass, "glowmarkt:electricity_consumption")
        window = _hours(REFRESH_START)
        assert len(usage) == hours
        assert usage[-window - 1][2] == pytest.approx((hours - window) * 1.0)
        assert usage[-1][2] == pytest.approx((hours - window) * 1.0 + window * 2.0)

    # Local midnight (BST) is 23:00 UTC, so today is 23:00-12:00 UTC: 14 hours.
    assert hass.states.get("sensor.bright_electricity_usage_today").state == "28.0"
    assert hass.states.get("sensor.bright_electricity_cost_today").state == "7.35"


async def test_first_rate_tariff(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """The first 2 kWh after local midnight use the First Rate."""
    freezer.move_to(NOW)
    await hass.config.async_update(currency="GBP", time_zone="Europe/London")
    entry = MockConfigEntry(domain=DOMAIN, data=LOGIN, options=SMART_ENERGY)
    entry.add_to_hass(hass)
    meter = FakeMeter(1.5)
    with (
        patch(f"{CLIENT}.resources", return_value=RESOURCES),
        patch(f"{CLIENT}.catchup", return_value=None),
        patch(f"{CLIENT}.readings", side_effect=meter.readings),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Refresh too, so the re-imported window is checked as well.
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    first, saver = 0.4723, 0.2578
    cost = {ts: c for ts, c, _ in await _sums(hass, "glowmarkt:electricity_cost")}
    midnight = datetime(2026, 10, 18, 23, tzinfo=UTC)  # 19 Oct 00:00 BST
    # 00:00 1.5 kWh first; 01:00 0.5 first + 1.0 saver; 02:00 all saver.
    assert cost[midnight] == pytest.approx(1.5 * first)
    assert cost[midnight + timedelta(hours=1)] == pytest.approx(0.5 * first + saver)
    assert cost[midnight + timedelta(hours=2)] == pytest.approx(1.5 * saver)
    # The hour before midnight is the previous day's saver rate.
    assert cost[midnight - timedelta(hours=1)] == pytest.approx(1.5 * saver)

    # Whole days: 2 kWh at First Rate + 34 kWh at Saver Rate each day.
    sums = await _sums(hass, "glowmarkt:electricity_cost")
    by_ts = {ts: total for ts, _, total in sums}
    day_cost = by_ts[midnight + timedelta(hours=23)] - by_ts[midnight - timedelta(hours=1)]
    assert day_cost == pytest.approx(2 * first + 34 * saver)

    # Today so far: 14 hours x 1.5 kWh = 21 kWh, 2 at First Rate.
    expected = round(2 * first + 19 * saver, 2)
    assert hass.states.get("sensor.bright_electricity_cost_today").state == str(expected)
