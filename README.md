# YouTube Niche Discovery Dashboard

A local Streamlit dashboard that discovers recently-created YouTube channels by searching for recent high-view videos, then validating the channel creation date.

## Requirements
- Python 3.10 or newer
- A Google Cloud project with **YouTube Data API v3** enabled
- An API key for that project

## Install and run (Windows)
Open Command Prompt or PowerShell in this folder and run:

```powershell
py -m venv .venv
.venv\Scripts\activate
py -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```

## Install and run (Linux / macOS)
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```

Then paste your YouTube Data API v3 key into the sidebar. You can also set `YOUTUBE_API_KEY` as an environment variable before launching the app.

## Implemented filters
- Channel creation age: default 7–90 days
- At least one video with 20,000+ views (configurable)
- Excludes channels whose public country code is PK, IN, BD, or LK
- Excludes videos whose declared language/audio-language tag matches the configured South Asian language-code list
- Optional allow-list of country codes
- Unknown country is hidden by default in the results view; enable the checkbox to inspect unknown-country channels
- SQLite persistence and CSV export

## Important accuracy and quota notes
1. The app does **not** enumerate every YouTube channel. It searches videos for the entered query phrases, then validates channels found through those videos. Broader query coverage requires more API calls.
2. YouTube Data API `search.list` is quota-expensive (commonly 100 quota units per request under the standard quota allocation). The default is one page per query. Start with a few queries and check quota in Google Cloud Console before running the full query list.
3. `publishedAfter` limits the searched videos to the maximum channel-age window. The channel's actual creation date is separately checked against 7–90 days.
4. YouTube does not reliably expose a channel's country or language. Missing values remain Unknown. The app does not guess a premium country from the language or title.
5. Language exclusion is based on metadata fields that may be absent or inaccurate. It cannot reliably detect the spoken language of every video. Manual review is required for uncertain records.
6. The four excluded country codes are always filtered out when the channel country is supplied. Unknown-country channels are not shown unless the user enables them.
7. Subscriber counts can be hidden or rounded by YouTube; the app does not fabricate them.

## Data storage
The app creates `youtube_discovery.db` in the current working directory. It contains discovered channel/video metadata. The API key is not stored in SQLite.

## Troubleshooting
- `quotaExceeded` / `dailyLimitExceeded`: wait for quota reset or request more quota in Google Cloud Console. Reduce search phrases/pages.
- `API key not valid`: verify the key and that YouTube Data API v3 is enabled for its Google Cloud project.
- No results: try fewer, broader queries; check that the API key has quota; optionally enable unknown-country channels.
- Port in use: run `streamlit run app.py --server.port 8502`.
