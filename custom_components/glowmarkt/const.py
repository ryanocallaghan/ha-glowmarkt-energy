"""Constants for the Glowmarkt (Bright) integration."""

from datetime import timedelta
import logging

DOMAIN = "glowmarkt"
LOGGER = logging.getLogger(__package__)

# Fuel name -> Glowmarkt resource classifier (both are reported in kWh).
FUELS = {
    "electricity": "electricity.consumption",
    "gas": "gas.consumption",
}

# Option keys, per fuel, e.g. f"{fuel}_{CONF_UNIT_RATE}".
CONF_UNIT_RATE = "unit_rate"  # p/kWh (the Saver Rate on a two-tier tariff)
CONF_STANDING_CHARGE = "standing_charge"  # p/day
# Optional first tier: the first CONF_FIRST_RATE_KWH kWh after local midnight
# each day cost CONF_FIRST_RATE p/kWh (e.g. Utilita Smart Energy).
CONF_FIRST_RATE = "first_rate"  # p/kWh
CONF_FIRST_RATE_KWH = "first_rate_kwh"  # kWh/day, 0 = off

UPDATE_INTERVAL = timedelta(minutes=30)
# How far back to import on first run.
BACKFILL = timedelta(days=60)
# DCC data arrives late, so every update re-imports this trailing window.
REFRESH_WINDOW = timedelta(days=3)
# Maximum span requested from the API in one call.
CHUNK = timedelta(days=10)
