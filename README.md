# Late Checkout

A price finder for Amex Fine Hotels + Resorts (FHR) and The Hotel Collection (THC). It shows the
cheapest nights at each hotel and what you pay after the Platinum hotel credit. Prices refresh by
themselves once a day.

## How it works

```
data/catalog.json            3,106 hotels, each with its Google Hotels id
        |
scripts/update_prices.py     daily: one search per hotel returns its whole price calendar
        |
site/data/prices.json        nightly rates for the next 240 days, with the time each hotel was checked
        |
site/index.html              the page; reads prices.json, shows sample data until prices exist
```

`.github/workflows/update-prices.yml` runs the updater every day on GitHub's servers, saves the
prices, and republishes the site with GitHub Pages. Nothing runs on your own computer.

## Set up (about 10 minutes)

1. **Create a repository** on GitHub and put these files in it. Make it public: free GitHub Pages
   needs a public repository.
2. **Add your SerpApi key.** In the repository: Settings, then Secrets and variables, then Actions,
   then "New repository secret". Name it `SERPAPI_KEY` and paste the key from your SerpApi account.
3. **Turn on Pages.** Settings, then Pages, then set Source to "GitHub Actions".
4. **Do a small test run.** Actions tab, "Update prices and publish site", "Run workflow", and type
   `5` in the searches box. It prices five hotels and publishes the site. The run's summary shows
   what happened and how many searches are left.
5. Open the site address shown on the Pages settings screen.

After that it runs daily at about 2:30 am Pacific.

## Budget: how many hotels stay fresh

One search prices one hotel for the whole 240 days. Each daily run spends `monthly_searches / 31`.

| SerpApi plan | Searches a month | Hotels a day | Hotels you can keep under a week old | All 3,104 hotels refreshed every |
|---|---|---|---|---|
| Free | 250 | 8 | 56 | about 388 days |
| $25 | 1,000 | 32 | 224 | about 97 days |
| $75 | 5,000 | 161 | 1,127 | about 19 days |
| $150 | 15,000 | 483 | all | about 6 days |
| $275 | 30,000 | 967 | all | about 3 days |

Plan prices are from serpapi.com/pricing in November 2026. On a small plan, narrow the scope in
`config.json` so the hotels you care about stay fresh.

## config.json

| Setting | What it does |
|---|---|
| `monthly_searches` | Your plan's monthly searches. The updater never spends more than 1/31 of this per day, and never more than the account has left. |
| `countries` | Only track these countries, for example `["United States", "Japan"]`. Empty means everywhere. |
| `regions` | Only track these regions: "United States and Canada", "Europe", "Asia", "Mexico and Caribbean", "Middle East and Africa", "Australia and Pacific", "Central and South America". |
| `programs` | `["FHR", "THC"]`, or just one of them. |
| `watchlist` | Hotel names (or part of a name) to refresh every day ahead of everything else, even outside the scope above. |
| `max_hotels` | Hard cap on hotels tracked. 0 means no cap. |
| `focus_max_rate`, `focus_weight` | Hotels whose cheapest night is at or under `focus_max_rate` are refreshed `focus_weight` times as often, since those are the ones worth spending the credit on. |
| `watch_refresh_hours` | How old a watchlist hotel's prices may get before they are refreshed. |
| `horizon_days` | How far ahead to fetch prices. |
| `default_tax` | Tax and fee rate assumed when Google gives no after-tax price for a hotel. |

How each run chooses hotels: watchlist first; then half the budget goes to hotels with no prices
yet and half to the stalest priced hotels. A hotel that fails three times in a row is skipped for 30 days.

## Run it yourself

```
export SERPAPI_KEY=your-key
python3 scripts/update_prices.py --dry-run          # show what would be fetched, spend nothing
python3 scripts/update_prices.py --max-searches 5   # price five hotels
python3 -m http.server --directory site 8000        # then open http://localhost:8000
python3 scripts/build_catalog.py                    # refresh the hotel list (also runs every Monday)
```

Python 3.9 or newer, no packages to install.

## What to know before relying on it

- **The price is a stand-in.** It is the best one-night price listed on Google Hotels, before tax.
  The Amex rate is usually close to the hotel's own flexible rate, but Google's best price can be a
  cheaper member or non-refundable rate. Every hotel links to its Amex page to confirm.
- **Not yet run against the live API.** The updater was tested against a local stand-in built from
  SerpApi's documentation for the Price Calendar API, which is new (October 2026). Step 4 above is
  the real first test.
- **Taxes.** Where Google gives an after-tax price, the site uses that hotel's real tax-and-fee
  ratio. Otherwise it uses `default_tax` and labels the line "Estimated".
- **Free-night offers** (third or fourth night free) are not in the hotel list, so the live site
  does not show them.
- **GitHub pauses schedules** on repositories with no activity for 60 days. It emails first, and one
  click turns the schedule back on.
- Prices are kept on a `data` branch that is overwritten each run, so the repository stays small.

See NOTICE.md for data credits.
