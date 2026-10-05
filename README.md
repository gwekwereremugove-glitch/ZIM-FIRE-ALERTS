# Zimbabwe Wildfire Alerts (NASA FIRMS → SMS / WhatsApp)

Runs every 20 min on GitHub Actions. Checks FIRMS (VIIRS S-NPP, NOAA-20, NOAA-21, MODIS), keeps points inside
your district polygons, alerts each province's stakeholders (max 5 + optional admin `*`) about NEW fires only.

## Repo layout
```
fire_alerts.py                    main script
requirements.txt
.github/workflows/fire-alerts.yml scheduler
data/Districts.*                  your ward shapefile (+ optional districts_simplified.geojson)
prepare_districts.py              one-off: wards -> small district GeoJSON
config/stakeholders.example.json  template (real numbers go in a secret)
state/alerted.json                dedup memory (auto-committed)
```

## Setup (beginner)

### 1. Get a FIRMS MAP_KEY
1. Go to https://firms.modaps.eosdis.nasa.gov/api/map_key/
2. Enter your email, accept, submit. The key (32 characters) arrives by email within minutes.

### 2. Create a Twilio account (SMS + WhatsApp)
1. Sign up at https://www.twilio.com and verify your own phone.
2. Console home: copy **Account SID** and **Auth Token**.
3. **SMS**: buy/get a Twilio number (Phone Numbers > Buy). Check Zimbabwe (+263) is enabled under
   Messaging > Settings > Geo permissions. Trial accounts can only text numbers you have *verified*, and
   some countries need a registered sender ID - check Twilio's Zimbabwe guidelines. Upgrade the account for real use.
4. **WhatsApp (testing)**: Messaging > Try it out > Send a WhatsApp message. Each recipient must send the
   "join <code>" message to the sandbox number first. Sandbox sender is `+14155238886`.
   **WhatsApp (production)**: you need an approved WhatsApp sender and *approved message templates*
   (free-form messages only work within 24 h of the person's last message). This is the biggest hurdle for
   unattended alerts - SMS is the more reliable channel.

### 3. Create the repo
1. github.com > New repository. **Public** gives unlimited Actions minutes (private repos get 2,000 min/month,
   and 72 runs/day is ~2,160+ min - use `*/30` if you go private). Public is safe here because phone numbers live in a Secret.
2. Upload all files from this folder (drag & drop on the web page works; make sure `.github/workflows/fire-alerts.yml` keeps its path).
3. Your shapefile (`Districts.shp/.dbf/.shx/.prj`) is already in `data/`. It is a **ward-level** file (1,752 wards, columns
   `PROVINCE`, `DISTRICT`, CRS WGS84), and the script merges wards into 63 districts automatically.
   Recommended: shrink it once on your computer (`pip install geopandas pyogrio`, then
   `python prepare_districts.py data/Districts.shp data/districts_simplified.geojson`), commit the small GeoJSON,
   and delete the 29 MB `Districts.*` files. The script uses `districts_simplified.geojson` first if it exists.
   Upload all four shapefile parts together if you keep the original (GitHub web upload limit is 25 MB per file via the browser,
   so use GitHub Desktop / `git push` for the 29 MB `.shp`, or just upload the small GeoJSON).
4. Underscores in names are converted to spaces (`Mashonaland_West` -> `Mashonaland West`). Province names must match the keys in your stakeholders JSON (case-insensitive).

### 4. Add Secrets
Repo > Settings > Secrets and variables > Actions > New repository secret:

| Secret | Value |
|---|---|
| `FIRMS_MAP_KEY` | your MAP_KEY |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | from Twilio console |
| `TWILIO_SMS_FROM` | your Twilio number, e.g. `+1415...` |
| `TWILIO_WHATSAPP_FROM` | e.g. `+14155238886` (sandbox) or your approved sender |
| `STAKEHOLDERS_JSON` | the full contents of your edited stakeholders file (see `config/stakeholders.example.json`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | *optional* free test channel (below) |

Also: Settings > Actions > General > Workflow permissions > **Read and write permissions** (needed to commit the state file).

### Optional: free Telegram test channel
In Telegram, message **@BotFather** > `/newbot` > copy the token. Send any message to your new bot, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`. Add both as secrets.
You get every alert (all provinces) there - ideal for testing without paying for SMS.

### 5. First test run
1. Actions tab > **Wildfire alerts** > **Run workflow**. Leave **dry_run = true** > Run. Open the run log: you should
   see rows per satellite, detections inside districts, and the exact messages that *would* be sent.
2. Run again with dry_run = false, **baseline = true**: records all current fires as "seen" with no alerts (avoids a flood on day 1).
3. Run once with both false to test real sending (or wait for a new fire). After that the 20-minute schedule takes over.
   Each run appears in the Actions tab; a summary is on the run page.

## What an alert contains
Province, number of fires, and per fire: district, lat/lon, local time (CAT), satellite(s), confidence, FRP,
approx. size (pixel count and hectares footprint), position **relative to the district centre** (e.g. "39 km NW"),
**wind-based spread direction** (current wind from Open-Meteo: fire spreads toward the direction the wind blows to),
and a Google Maps link.
Note: satellites cannot tell which way a fire is "coming from"; the direction shown is (a) where the fire is inside the district
and (b) where the wind will likely push it. Size is the satellite pixel footprint (375 m VIIRS / 1 km MODIS) - an upper
bound on the burning area, not a burnt-area measurement. Small/moderate/large uses peak FRP as a rough guide.

## How duplicates are handled
* Exact same detection (sensor + position + time) is never alerted twice.
* A fire stays visible for hours and is re-detected on each pass. A detection within 1.5 km of an alert from the last 24 h is treated as the
  same fire and not re-alerted (`SUPPRESS_HOURS=0` to alert every detection). A spreading front can therefore stay suppressed.
* If sending fails for everyone in a province, those fires are retried next run.
* Low-confidence detections are dropped by default (`MIN_CONFIDENCE=low` to keep them).

## Limitations of FIRMS data
* **Overpass frequency**: polar-orbiting satellites only. Over Zimbabwe, VIIRS (S-NPP, NOAA-20, NOAA-21) plus MODIS (Terra, Aqua)
  give roughly 6-10 passes/day in total, mostly clustered around ~01:30 and ~13:30 local solar time, with NOAA-20/21 about
  half an orbit apart from S-NPP. So new data usually arrives in bursts a few times a day, not every 20 minutes. Fires that start
  and die between passes (especially at night in the gaps) are never seen.
* **Latency**: NRT data is typically available ~3 hours after observation (often faster for VIIRS/MODIS: roughly 1-3 h). The detection time in
  the alert is the satellite overpass time, so the fire is already some hours old. This is not an emergency-dispatch system.
* **Spatial accuracy**: 375 m (VIIRS) / 1 km (MODIS) pixels. The coordinate is the pixel centre; the fire may be anywhere in the pixel.
* **False detections**: hot bare ground, gas flares, industrial sites (mines, smelters), agricultural burning and sun glint can trigger
  detections; planned burns and fire-breaks are real fires but may be wanted. Cloud and thick smoke hide fires (omission errors).
  Low-confidence points are filtered by default; treat alerts as "verify on the ground".
* **Size/FRP**: FRP (MW) is a snapshot at overpass, not total fire size. Hectares shown are pixel footprint.
* **GitHub Actions**: the 20-minute cron can run late (5-15 min) under load, and scheduled workflows in repos with no activity for 60 days
  may be paused (re-enable in the Actions tab). The FIRMS key allows 5,000 requests per 10 min - this uses 4 every 20 min.
