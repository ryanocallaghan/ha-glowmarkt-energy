# Glowmarkt (Bright) Energy for Home Assistant

Imports half-hourly smart meter data from the [Bright app](https://glowmarkt.com) (Hildebrand Glowmarkt / DCC) into Home Assistant's Energy dashboard, with costs worked out from your own tariff.

Works for any UK supplier whose SMETS2 (or DCC-migrated SMETS1) meter shows up in Bright, including Utilita pay-as-you-go.

## What you get

| Statistic (for the Energy dashboard) | What it is |
| --- | --- |
| `glowmarkt:electricity_consumption` | Hourly electricity use, kWh |
| `glowmarkt:electricity_cost` | Unit rate × kWh + standing charge, spread evenly over the day |
| `glowmarkt:gas_consumption` / `glowmarkt:gas_cost` | The same for gas, if you have a gas smart meter |

Also a few sensors for ordinary dashboards: **usage today**, **cost today** and **latest data** (the newest hour Bright has data for) per fuel.

The DCC delivers readings hours late. That's why the data comes in as statistics stamped at the hour it was used, not as a live sensor. Each update re-imports the last 3 days, so late readings fill in.

## History

Set **History to import (months)** during setup or under **Configure**. The default is 12; **0 imports everything Bright has**, from your meter's first reading.

History loads in the background after setup, in 10-day chunks every 15 seconds, so a year takes about 10 minutes. The Energy dashboard fills in as it goes. Raising the setting later re-imports all history from the new start date and recalculates its costs at your current prices. Lowering it keeps what's already imported.

## Install

**HACS (custom repository):** in HACS → ⋮ → Custom repositories add `https://github.com/ryanocallaghan/ha-glowmarkt-energy` with type *Integration*, install **Glowmarkt (Bright) Energy**, and restart.

**Manual:** copy `custom_components/glowmarkt` into your Home Assistant `config/custom_components/` folder and restart.

Then go to **Settings → Devices & services → Add integration → Glowmarkt (Bright) Energy**. Log in with your Bright email and password, then choose how much history to load and enter your tariff in pence, including VAT. Change the tariff any time with the integration's **Configure** button.

## Energy dashboard

**Settings → Dashboards → Energy → Add consumption** (electricity grid):
- Energy: `Bright Electricity consumption`
- Cost: *Use an entity tracking the total costs* → `Bright Electricity cost`

Do the same under **Gas consumption** for gas.

## If you used the old Hildebrand Glow integration

The two can run side by side; this one uses different statistic IDs. Once you're happy, remove the old one and swap the Energy dashboard sources over. History stays under the old statistics, so you can delete those under **Developer tools → Statistics** if you don't want both.

## Check your login outside Home Assistant

```bash
pip install requests
python3 scripts/check_api.py
```

It prints yesterday's hourly usage for each meter.

## Two-tier tariffs (e.g. Utilita Smart Energy)

Some tariffs have no standing charge. Instead, the first few kWh each day cost more. For each fuel, set:

| Field | Utilita Smart Energy example (electricity / gas) |
| --- | --- |
| Unit rate | Saver Rate: 25.78 / 7.89 p/kWh |
| Standing charge | 0 |
| First Rate | 47.23 / 22.97 p/kWh |
| First Rate allowance per day | 2 kWh |

The allowance resets at local midnight. Leave it at 0 for an ordinary single-rate tariff.

## Notes

- Tariff changes apply from 3 days back onward. Older cost history keeps the price it was imported with.
- The standing charge is spread as 1/24 per hour, so on the two clock-change days the daily total is off by one hour's share.
- Uses the Bright app's public application ID, like other community clients. This is not an official Hildebrand integration.
