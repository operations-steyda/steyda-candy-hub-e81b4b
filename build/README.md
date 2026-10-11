# Automated dashboard refresh

A GitHub Action (`.github/workflows/refresh.yml`) refreshes the dashboard's
**PPC + keyword** data from the DataDoe REST API once a day and republishes.

## What it does
`build/refresh.py`:
1. Pulls PPC campaign data, the search-term report, and FBA 90-day shipped sales
   from the DataDoe REST API (`https://api.datadoe.com/api/v1`).
2. Rebuilds the `ADS` and `KEYWORDS` data constants (with a fresh TACOS
   denominator from the FBA 90-day sales).
3. **Decrypts** the live `index.html` with the site password, splices in the
   fresh constants, and **re-encrypts** it. The plaintext dashboard is never
   written to the repo — nothing leaks.
4. Skips the commit entirely if the data is unchanged.

Only `ADS` + `KEYWORDS` are touched. **Inventory, replenishment, Joar warehouse,
inbound and sales are left exactly as the last manual run left them** — run
`refresh the candy dashboard` in Claude Code to update those (Phase 2 will
automate the inventory model once its math is verified).

## One-time setup — add two repository secrets
GitHub → the repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|------|-------|
| `DATADOE_API_KEY` | your DataDoe REST API key (`dd_api_…`) from app.datadoe.com → Integrations → REST API |
| `SITE_PW` | the dashboard's unlock password |

That's it. The Action runs daily (~06:00 Sweden), and you can trigger it any time
from the **Actions** tab → *Daily dashboard refresh* → **Run workflow**.

## Changing the schedule
Edit the `cron` line in `.github/workflows/refresh.yml` (times are UTC).
