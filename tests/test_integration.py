"""Tests for the config flow, options flow and statistics import."""

import asyncio
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
    "history_months": 2,
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
    "history_months": 1,
    "electricity_unit_rate": 25.78,
    "electricity_standing_charge": 0,
    "electricity_first_rate": 47.23,
    "electricity_first_rate_kwh": 2,
}
NOW = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
LAST_HOUR = NOW.replace(minute=0)
# Local (BST) midnights, in UTC.
TWO_MONTHS_AGO = datetime(2026, 8, 19, 23, tzinfo=UTC)
THREE_MONTHS_AGO = datetime(2026, 7, 19, 23, tzinfo=UTC)
REFRESH_START = datetime(2026, 10, 16, 23, tzinfo=UTC)


def _hours(start: datetime) -> int:
    return int((LAST_HOUR - start) / timedelta(hours=1)) + 1


class FakeMeter:
    """Return a fixed kWh value for every hour asked for."""

    def __init__(self, kwh: float) -> None:
        self.kwh = kwh
        self.calls = 0

    async def readings(self, resource_id, start, end, period="PT1H"):
        self.calls += 1
        assert end - start <= timedelta(days=10)
        hour = start.replace(minute=0, second=0, microsecond=0)
        out = []
        while hour <= end:
            out.append((hour, self.kwh))
            hour += timedelta(hours=1)
        return out


@pytest.fixture
def meter():
    """Patch the API client with a fake meter."""
    meter = FakeMeter(1.0)
    with (
        patch(f"{CLIENT}.resources", return_value=RESOURCES),
        patch(f"{CLIENT}.catchup", return_value=None),
        patch(f"{CLIENT}.first_reading", return_value=datetime(2026, 10, 1, 9, tzinfo=UTC)),
        patch(f"{CLIENT}.readings", side_effect=meter.readings),
    ):
        yield meter


async def _setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory, options) -> MockConfigEntry:
    freezer.move_to(NOW)
    await hass.config.async_update(currency="GBP", time_zone="Europe/London")
    entry = MockConfigEntry(domain=DOMAIN, data=LOGIN, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _finish_backfill(hass: HomeAssistant, entry: MockConfigEntry) -> int:
    """Run updates until history is imported; return how many it took."""
    coordinator = entry.runtime_data
    runs = 0
    while coordinator.backfilling:
        runs += 1
        assert runs < 100
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    return runs


async def _stats(hass: HomeAssistant, statistic_id: str) -> list[tuple[datetime, float, float]]:
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        NOW - timedelta(days=400),
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


async def test_setup_is_quick_then_backfills_in_chunks(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, meter: FakeMeter
) -> None:
    """Setup fetches one chunk; later updates fill in the rest, gap-free."""
    entry = await _setup(hass, freezer, CHARGES)
    assert meter.calls == 1
    assert entry.runtime_data.backfilling
    assert entry.runtime_data.update_interval == timedelta(seconds=15)

    runs = await _finish_backfill(hass, entry)
    assert runs >= 6  # ~62 days in 10-day chunks, then one normal update
    assert entry.runtime_data.update_interval == timedelta(minutes=30)

    usage = await _stats(hass, "glowmarkt:electricity_consumption")
    cost = await _stats(hass, "glowmarkt:electricity_cost")
    hours = _hours(TWO_MONTHS_AGO)
    assert usage[0][0] == TWO_MONTHS_AGO
    assert len(usage) == hours
    assert all(b[0] - a[0] == timedelta(hours=1) for a, b in zip(usage, usage[1:]))
    assert usage[-1][2] == pytest.approx(hours * 1.0)
    assert cost[-1][2] == pytest.approx(hours * (0.25 + 0.60 / 24))

    # Late data: the last 3 days now read 2 kWh/h. Totals before stay put.
    meter.kwh = 2.0
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    usage = await _stats(hass, "glowmarkt:electricity_consumption")
    window = _hours(REFRESH_START)
    assert len(usage) == hours
    assert usage[-window - 1][2] == pytest.approx((hours - window) * 1.0)
    assert usage[-1][2] == pytest.approx((hours - window) * 1.0 + window * 2.0)

    # Local midnight (BST) is 23:00 UTC, so today is 23:00-12:00 UTC: 14 hours.
    assert hass.states.get("sensor.bright_electricity_usage_today").state == "28.0"
    assert hass.states.get("sensor.bright_electricity_cost_today").state == "7.35"


async def test_more_history_reimports_from_new_start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, meter: FakeMeter
) -> None:
    """Raising history_months later rebuilds the totals from the older start."""
    entry = await _setup(hass, freezer, CHARGES)
    await _finish_backfill(hass, entry)

    hass.config_entries.async_update_entry(entry, options={**CHARGES, "history_months": 3})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.backfilling
    await _finish_backfill(hass, entry)

    usage = await _stats(hass, "glowmarkt:electricity_consumption")
    hours = _hours(THREE_MONTHS_AGO)
    assert usage[0][0] == THREE_MONTHS_AGO
    assert len(usage) == hours
    assert usage[-1][2] == pytest.approx(hours * 1.0)


async def test_zero_months_imports_everything(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, meter: FakeMeter
) -> None:
    """history_months = 0 starts at the meter's first reading."""
    entry = await _setup(hass, freezer, {**CHARGES, "history_months": 0})
    await _finish_backfill(hass, entry)
    usage = await _stats(hass, "glowmarkt:electricity_consumption")
    first_midnight = datetime(2026, 9, 30, 23, tzinfo=UTC)  # 1 Oct 00:00 BST
    assert usage[0][0] == first_midnight
    assert usage[-1][2] == pytest.approx(_hours(first_midnight) * 1.0)


async def test_first_rate_tariff(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, meter: FakeMeter
) -> None:
    """The first 2 kWh after local midnight use the First Rate."""
    meter.kwh = 1.5
    entry = await _setup(hass, freezer, SMART_ENERGY)
    await _finish_backfill(hass, entry)
    # Refresh too, so the re-imported window is checked as well.
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    first, saver = 0.4723, 0.2578
    rows = await _stats(hass, "glowmarkt:electricity_cost")
    cost = {ts: c for ts, c, _ in rows}
    total = {ts: s for ts, _, s in rows}
    # 19 Oct 00:00 BST is in the re-imported window; 1 Oct is from backfill.
    for midnight in (
        datetime(2026, 10, 18, 23, tzinfo=UTC),
        datetime(2026, 9, 30, 23, tzinfo=UTC),
    ):
        # 00:00 1.5 kWh first; 01:00 0.5 first + 1.0 saver; 02:00 all saver.
        assert cost[midnight] == pytest.approx(1.5 * first)
        assert cost[midnight + timedelta(hours=1)] == pytest.approx(0.5 * first + saver)
        assert cost[midnight + timedelta(hours=2)] == pytest.approx(1.5 * saver)
        assert cost[midnight - timedelta(hours=1)] == pytest.approx(1.5 * saver)
        # A whole day: 2 kWh at First Rate + 34 kWh at Saver Rate.
        day_cost = total[midnight + timedelta(hours=23)] - total[midnight - timedelta(hours=1)]
        assert day_cost == pytest.approx(2 * first + 34 * saver)

    # Today so far: 14 hours x 1.5 kWh = 21 kWh, 2 at First Rate.
    expected = round(2 * first + 19 * saver, 2)
    assert hass.states.get("sensor.bright_electricity_cost_today").state == str(expected)


async def test_hanging_catchup_does_not_block_setup(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, meter: FakeMeter
) -> None:
    """The DCC catchup call runs in the background (it can hang for minutes)."""
    stuck = asyncio.Event()

    async def hang(self, resource_id):
        await stuck.wait()

    with patch(f"{CLIENT}.catchup", hang):
        freezer.move_to(NOW)
        entry = MockConfigEntry(domain=DOMAIN, data=LOGIN, options=CHARGES)
        entry.add_to_hass(hass)
        async with asyncio.timeout(5):
            assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)
