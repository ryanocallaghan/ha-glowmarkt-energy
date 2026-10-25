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

# How many months of history to import; 0 = everything Bright has.
CONF_HISTORY_MONTHS = "history_months"
DEFAULT_HISTORY_MONTHS = 12
# Used for 0 when Bright doesn't say when the meter's data starts.
FALLBACK_HISTORY_MONTHS = 24

UPDATE_INTERVAL = timedelta(minutes=30)
# While importing history, one chunk per update at this interval.
BACKFILL_INTERVAL = timedelta(seconds=15)
RETRY_INTERVAL = timedelta(minutes=5)
# How often to ask the DCC to fetch fresh data from the meter, in seconds.
CATCHUP_INTERVAL = 6 * 3600
# DCC data arrives late, so every update re-imports this trailing window.
REFRESH_WINDOW = timedelta(days=3)
# Span of one history chunk and of one API request.
CHUNK = timedelta(days=10)
