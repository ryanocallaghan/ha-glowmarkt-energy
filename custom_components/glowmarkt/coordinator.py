"""Fetch Glowmarkt readings and import them as long-term statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GlowmarktAuthError, GlowmarktClient, GlowmarktError
from .const import (
    BACKFILL,
    CHUNK,
    CONF_FIRST_RATE,
    CONF_FIRST_RATE_KWH,
    CONF_STANDING_CHARGE,
    CONF_UNIT_RATE,
    DOMAIN,
    FUELS,
    LOGGER,
    REFRESH_WINDOW,
    UPDATE_INTERVAL,
)


@dataclass
class FuelSummary:
    """What the sensors show for one fuel."""

    usage_today: float
    cost_today: float | None
    latest_data: datetime | None


@dataclass
class Tariff:
    """Prices for one fuel, in currency units (not pence)."""

    unit_rate: float
    standing_per_hour: float
    first_rate: float = 0.0
    first_rate_kwh: float = 0.0

    def cost(self, kwh: float, used_today: float) -> float:
        """Cost of kwh used in one hour, given kWh already used since midnight."""
        first = min(max(self.first_rate_kwh - used_today, 0.0), kwh)
        return (
            first * self.first_rate
            + (kwh - first) * self.unit_rate
            + self.standing_per_hour
        )


def statistic_ids(fuel: str) -> tuple[str, str]:
    """Return the (consumption, cost) statistic IDs for a fuel."""
    return f"{DOMAIN}:{fuel}_consumption", f"{DOMAIN}:{fuel}_cost"


class GlowmarktCoordinator(DataUpdateCoordinator[dict[str, FuelSummary]]):
    """Poll Glowmarkt and keep the recorder's statistics up to date."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: GlowmarktClient
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.resources: dict[str, str] = {}

    async def _async_setup(self) -> None:
        """Find the consumption resource for each fuel on the account."""
        try:
            found = await self.client.resources()
        except GlowmarktAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GlowmarktError as err:
            raise UpdateFailed(str(err)) from err
        by_classifier = {r.get("classifier"): r.get("resourceId") for r in found}
        self.resources = {
            fuel: by_classifier[classifier]
            for fuel, classifier in FUELS.items()
            if by_classifier.get(classifier)
        }
        if not self.resources:
            raise UpdateFailed("No electricity or gas meter found on this account")

    async def _async_update_data(self) -> dict[str, FuelSummary]:
        """Import new readings for every fuel."""
        try:
            return {
                fuel: await self._update_fuel(fuel, resource_id)
                for fuel, resource_id in self.resources.items()
            }
        except GlowmarktAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GlowmarktError as err:
            raise UpdateFailed(str(err)) from err

    def _tariff(self, fuel: str) -> Tariff | None:
        """Return the fuel's tariff, or None if no prices are set."""
        options = self.config_entry.options

        def get(key: str) -> float:
            return float(options.get(f"{fuel}_{key}") or 0)

        first_rate_kwh = get(CONF_FIRST_RATE_KWH)
        tariff = Tariff(
            unit_rate=get(CONF_UNIT_RATE) / 100,
            standing_per_hour=get(CONF_STANDING_CHARGE) / 100 / 24,
            first_rate=get(CONF_FIRST_RATE) / 100 if first_rate_kwh else 0.0,
            first_rate_kwh=first_rate_kwh,
        )
        if not (tariff.unit_rate or tariff.standing_per_hour or tariff.first_rate):
            return None
        return tariff

    async def _update_fuel(self, fuel: str, resource_id: str) -> FuelSummary:
        """Fetch recent readings for one fuel and import them."""
        usage_id, cost_id = statistic_ids(fuel)
        recorder = get_instance(self.hass)
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

        last = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, usage_id, True, {"sum"}
        )
        if last.get(usage_id):
            last_start = dt_util.utc_from_timestamp(last[usage_id][0]["start"])
            start = min(last_start, now) - REFRESH_WINDOW
        else:
            start = now - BACKFILL
        # Start at local midnight so daily First Rate allowances add up correctly.
        start = dt_util.as_utc(dt_util.start_of_local_day(dt_util.as_local(start)))

        await self.client.catchup(resource_id)
        readings: list[tuple[datetime, float]] = []
        chunk_start = start
        while chunk_start <= now:
            chunk_end = min(chunk_start + CHUNK, now)
            readings.extend(
                await self.client.readings(resource_id, chunk_start, chunk_end)
            )
            chunk_start = chunk_end + timedelta(hours=1)
        readings = sorted({ts: v for ts, v in readings if ts >= start}.items())

        # Carry on the running totals from the hour before the re-imported window.
        previous = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(hours=1),
            start,
            {usage_id, cost_id},
            "hour",
            None,
            {"sum"},
        )
        usage_sum = (previous.get(usage_id) or [{}])[-1].get("sum") or 0.0
        cost_sum = (previous.get(cost_id) or [{}])[-1].get("sum") or 0.0

        tariff = self._tariff(fuel)
        today = dt_util.start_of_local_day()
        usage_today = 0.0
        cost_today = 0.0
        latest_data = None
        day = None
        used_in_day = 0.0
        usage_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        for ts, kwh in readings:
            if (local_day := dt_util.as_local(ts).date()) != day:
                day, used_in_day = local_day, 0.0
            usage_sum += kwh
            usage_stats.append(StatisticData(start=ts, state=kwh, sum=usage_sum))
            if kwh:
                latest_data = ts
            if ts >= today:
                usage_today += kwh
            if tariff:
                cost = tariff.cost(kwh, used_in_day)
                cost_sum += cost
                cost_stats.append(StatisticData(start=ts, state=cost, sum=cost_sum))
                if ts >= today:
                    cost_today += cost
            used_in_day += kwh

        name = fuel.capitalize()
        if usage_stats:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"Bright {name} consumption",
                    source=DOMAIN,
                    statistic_id=usage_id,
                    unit_class="energy",
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                ),
                usage_stats,
            )
        if cost_stats:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"Bright {name} cost",
                    source=DOMAIN,
                    statistic_id=cost_id,
                    unit_class=None,
                    unit_of_measurement=self.hass.config.currency,
                ),
                cost_stats,
            )
        LOGGER.debug(
            "Imported %d hours of %s from %s", len(usage_stats), fuel, start.isoformat()
        )
        return FuelSummary(
            usage_today=round(usage_today, 3),
            cost_today=round(cost_today, 2) if tariff else None,
            latest_data=latest_data,
        )
