"""Fetch Glowmarkt readings and import them as long-term statistics."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
import time

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GlowmarktAuthError, GlowmarktClient, GlowmarktError
from .const import (
    BACKFILL_INTERVAL,
    CATCHUP_INTERVAL,
    CHUNK,
    CONF_FIRST_RATE,
    CONF_FIRST_RATE_KWH,
    CONF_HISTORY_MONTHS,
    CONF_STANDING_CHARGE,
    CONF_UNIT_RATE,
    DEFAULT_HISTORY_MONTHS,
    DOMAIN,
    FALLBACK_HISTORY_MONTHS,
    FUELS,
    LOGGER,
    REFRESH_WINDOW,
    RETRY_INTERVAL,
    UPDATE_INTERVAL,
)


@dataclass
class FuelSummary:
    """What the sensors show for one fuel."""

    usage_today: float | None
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


def local_midnight(when: datetime) -> datetime:
    """Return the UTC time of local midnight at the start of when's local day."""
    return dt_util.as_utc(dt_util.start_of_local_day(dt_util.as_local(when)))


def months_before(day: date, months: int) -> date:
    """Return the same day of the month, months earlier (clamped to month end)."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class GlowmarktCoordinator(DataUpdateCoordinator[dict[str, FuelSummary]]):
    """Poll Glowmarkt and keep the recorder's statistics up to date.

    History is imported oldest first, one chunk per update, with updates
    every few seconds until it catches up. After that each update
    re-imports the last few days, because DCC readings arrive late.

    Progress per fuel is stored as {"from": ..., "cursor": ...}: statistics
    exist without gaps from "from" up to (not including) "cursor".
    """

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
        self.backfilling = False
        self._store: Store[dict[str, dict[str, str]]] = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}"
        )
        self._progress: dict[str, dict[str, str]] = {}
        self._first_reading: dict[str, datetime | None] = {}
        self._last_catchup: dict[str, float] = {}

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
        self._progress = await self._store.async_load() or {}

    async def _async_update_data(self) -> dict[str, FuelSummary]:
        """Import the next chunk (or the recent window) for every fuel."""
        data: dict[str, FuelSummary] = {}
        backfilling = False
        try:
            for fuel, resource_id in self.resources.items():
                self._start_catchup(fuel, resource_id)
                summary, still_backfilling = await self._update_fuel(fuel, resource_id)
                data[fuel] = summary
                backfilling |= still_backfilling
        except GlowmarktAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GlowmarktError as err:
            self.update_interval = RETRY_INTERVAL
            raise UpdateFailed(str(err)) from err
        finally:
            await self._store.async_save(self._progress)

        self.backfilling = backfilling
        self.update_interval = BACKFILL_INTERVAL if backfilling else UPDATE_INTERVAL
        return data

    def _start_catchup(self, fuel: str, resource_id: str) -> None:
        """Ask the DCC for fresh meter data, in the background now and then."""
        now = time.monotonic()
        if now - self._last_catchup.get(fuel, -math.inf) < CATCHUP_INTERVAL:
            return
        self._last_catchup[fuel] = now

        async def catchup() -> None:
            try:
                await self.client.catchup(resource_id)
            except GlowmarktError as err:
                LOGGER.debug("Catchup for %s failed: %s", fuel, err)

        self.config_entry.async_create_background_task(
            self.hass, catchup(), f"{DOMAIN} catchup {fuel}"
        )

    async def _history_start(self, resource_id: str) -> datetime:
        """Return local midnight of the oldest day to import."""
        months = int(
            self.config_entry.options.get(CONF_HISTORY_MONTHS, DEFAULT_HISTORY_MONTHS)
        )
        if not months:
            if resource_id not in self._first_reading:
                self._first_reading[resource_id] = await self.client.first_reading(
                    resource_id
                )
            if first := self._first_reading[resource_id]:
                return local_midnight(first)
            months = FALLBACK_HISTORY_MONTHS
        oldest = months_before(dt_util.now().date(), months)
        return dt_util.as_utc(dt_util.start_of_local_day(oldest))

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

    async def _update_fuel(
        self, fuel: str, resource_id: str
    ) -> tuple[FuelSummary, bool]:
        """Import one chunk for a fuel. Returns (summary, still backfilling)."""
        usage_id, cost_id = statistic_ids(fuel)
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        recent = local_midnight(now - REFRESH_WINDOW)

        # Start over if there is no progress yet or more history was asked for.
        history_start = await self._history_start(resource_id)
        progress = self._progress.get(fuel)
        if not progress or history_start < datetime.fromisoformat(progress["from"]):
            LOGGER.info("Importing %s history from %s", fuel, history_start.date())
            progress = {"from": history_start.isoformat(), "cursor": history_start.isoformat()}
            self._progress[fuel] = progress
        chain_start = datetime.fromisoformat(progress["from"])
        cursor = datetime.fromisoformat(progress["cursor"])

        backfilling = cursor < recent
        if backfilling:
            start = cursor
            # Ten local days on, landing on local midnight across clock changes.
            end = min(local_midnight(cursor + CHUNK + timedelta(hours=12)), recent)
            next_cursor = end
        else:
            start = min(cursor, recent)
            end = now + timedelta(hours=1)
            next_cursor = local_midnight(now)

        readings: list[tuple[datetime, float]] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + CHUNK, end)
            readings.extend(
                await self.client.readings(
                    resource_id, chunk_start, chunk_end - timedelta(hours=1)
                )
            )
            chunk_start = chunk_end
        readings = sorted({ts: v for ts, v in readings if start <= ts < end}.items())

        # Carry on the running totals from the hour before this chunk.
        usage_sum = cost_sum = 0.0
        if start > chain_start:
            recorder = get_instance(self.hass)
            await recorder.async_block_till_done()
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
            if not previous.get(usage_id):
                LOGGER.warning("No %s statistics before %s; totals restart", fuel, start)
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
        progress["cursor"] = next_cursor.isoformat()
        LOGGER.debug(
            "Imported %d hours of %s from %s", len(usage_stats), fuel, start.isoformat()
        )

        if backfilling:
            # Today isn't in this chunk; keep whatever the sensors showed before.
            previous_summary = (self.data or {}).get(fuel)
            # Report True even on the last chunk so a quick normal update follows.
            return previous_summary or FuelSummary(None, None, None), True
        return (
            FuelSummary(
                usage_today=round(usage_today, 3),
                cost_today=round(cost_today, 2) if tariff else None,
                latest_data=latest_data,
            ),
            False,
        )
