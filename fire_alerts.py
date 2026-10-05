#!/usr/bin/env python3
"""
Zimbabwe wildfire alert system (NASA FIRMS -> SMS / WhatsApp)
=============================================================

What one run does (GitHub Actions runs it every 20 minutes):
  1. Downloads near-real-time active-fire detections from the NASA FIRMS Area API
     (VIIRS S-NPP, NOAA-20, NOAA-21 and MODIS) for the bounding box of your districts.
  2. Keeps only points that fall INSIDE the real district polygons (geopandas).
  3. Drops detections already alerted (state/alerted.json) and "continuations" of fires
     alerted in the last 24 h (the same fire is re-detected on every satellite pass).
  4. Merges neighbouring detections (<=1 km) into one "fire event" per district.
  5. For each province, sends an SMS / WhatsApp message to that province's stakeholders:
     coordinates, time (Africa/Harare), satellite, confidence, FRP, approximate size,
     position relative to the district centre, wind-driven spread direction, Maps link.
  6. Saves the state file (the workflow commits it back to the repo).

Flags / environment:
  --dry-run   (or DRY_RUN=1)   print messages, send nothing, do not save state
  --baseline  (or BASELINE=1)  mark all current fires as seen WITHOUT alerting
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# CONFIG (override with environment variables where shown)
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent


def _find_districts_file() -> Path:
    """DISTRICTS_FILE env wins; otherwise the first of these that exists in data/."""
    env = os.getenv("DISTRICTS_FILE")
    if env:
        return Path(env)
   for name in ("districts_simplified.geojson", "zimbabwe_districts.geojson", "Districts.zip", "Districts.shp"):
        if (ROOT / "data" / name).exists():
            return ROOT / "data" / name
    return ROOT / "data" / "Districts.shp"


DISTRICTS_FILE = _find_districts_file()
STAKEHOLDERS_FILE = ROOT / "config" / "stakeholders.json"
STATE_FILE = ROOT / "state" / "alerted.json"

# FIRMS near-real-time sources. If one is renamed/unavailable it is logged and skipped.
SOURCES = {
    "VIIRS_SNPP_NRT": "S-NPP VIIRS",
    "VIIRS_NOAA20_NRT": "NOAA-20 VIIRS",
    "VIIRS_NOAA21_NRT": "NOAA-21 VIIRS",
    "MODIS_NRT": "MODIS",
}
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}"
DAY_RANGE = int(os.getenv("DAY_RANGE", "2"))        # 2 days covers the UTC-midnight rollover
BBOX_BUFFER_DEG = 0.05

# Field names in YOUR district file (auto-detected if left empty; set if detection fails)
DISTRICT_FIELD = os.getenv("DISTRICT_FIELD", "")
PROVINCE_FIELD = os.getenv("PROVINCE_FIELD", "")
DISTRICT_CANDIDATES = ["district", "adm2_en", "name_2", "dist_name", "districtna", "distname", "name"]
PROVINCE_CANDIDATES = ["province", "adm1_en", "name_1", "prov_name", "provincena", "provname", "prov"]

# False-alarm filter: "low" keeps everything, "nominal" drops low-confidence, "high" keeps high only
MIN_CONFIDENCE = os.getenv("MIN_CONFIDENCE", "nominal").lower()
CLUSTER_RADIUS_KM = 1.0          # detections closer than this = one fire event
SUPPRESS_RADIUS_KM = 1.5         # a new detection this close to a recent alert = same fire
SUPPRESS_HOURS = float(os.getenv("SUPPRESS_HOURS", "24"))   # 0 = alert on every new detection
STATE_RETENTION_DAYS = 7
MAX_EVENTS_WHATSAPP = 8
MAX_EVENTS_SMS = 3
WHATSAPP_MAX_CHARS = 1500        # Twilio limit is 1600
SMS_MAX_CHARS = 600
HTTP_TIMEOUT = (10, 60)          # (connect, read) seconds

TZ = ZoneInfo("Africa/Harare")   # CAT, UTC+2
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
CONF_LABELS = ["low", "nominal", "high"]

log = logging.getLogger("fire")


# --------------------------------------------------------------------------- #
# SMALL HELPERS
# --------------------------------------------------------------------------- #
def redact(text: object) -> str:
    """Hide secrets (MAP_KEY, tokens) from any text we log."""
    s = str(text)
    for name in ("FIRMS_MAP_KEY", "TWILIO_AUTH_TOKEN", "TELEGRAM_BOT_TOKEN"):
        val = os.getenv(name)
        if val:
            s = s.replace(val, "***")
    return s


def make_session() -> requests.Session:
    """HTTP session that retries on 429/5xx with exponential back-off."""
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def haversine_matrix(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Pairwise great-circle distance in km between two sets of points (arrays)."""
    la1, lo1 = np.radians(np.asarray(lat1, float)), np.radians(np.asarray(lon1, float))
    la2, lo2 = np.radians(np.asarray(lat2, float)), np.radians(np.asarray(lon2, float))
    dlat = la1[:, None] - la2[None, :]
    dlon = lo1[:, None] - lo2[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(la1)[:, None] * np.cos(la2)[None, :] * np.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial compass bearing from point 1 to point 2 (0 = N, 90 = E)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    x = np.sin(dl) * np.cos(p2)
    y = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return float((np.degrees(np.arctan2(x, y)) + 360) % 360)


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def maps_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat:.5f},{lon:.5f}"


def fmt_time(ts: pd.Timestamp) -> str:
    return ts.tz_convert(TZ).strftime("%d %b %H:%M") + " CAT"


# --------------------------------------------------------------------------- #
# STATE (deduplication)
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    """state = {"version":1, "alerted": {detection_id: [lat, lon, acq_epoch_seconds]}}"""
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        st.setdefault("alerted", {})
        return st
    except FileNotFoundError:
        return {"version": 1, "alerted": {}}
    except json.JSONDecodeError:
        log.error("State file is corrupt - starting empty (you may get repeat alerts once).")
        return {"version": 1, "alerted": {}}


def save_state(state: dict, original: dict) -> None:
    """Prune old entries; write only if something changed (avoids pointless commits)."""
    cutoff = time.time() - STATE_RETENTION_DAYS * 86400
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v[2] >= cutoff}
    if state == original:
        log.info("State unchanged.")
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, sort_keys=True, separators=(",", ":"))
    tmp.replace(STATE_FILE)
    log.info("State saved (%d tracked detections).", len(state["alerted"]))


# --------------------------------------------------------------------------- #
# DISTRICTS
# --------------------------------------------------------------------------- #
def _pick_field(columns, override, candidates, what):
    lower = {c.lower(): c for c in columns}
    if override:
        if override.lower() in lower:
            return lower[override.lower()]
        raise SystemExit(f"{what} field '{override}' not found. Columns: {list(columns)}")
    for c in candidates:
        if c in lower:
            return lower[c]
    raise SystemExit(f"Could not auto-detect the {what} column. Columns: {list(columns)}. "
                     f"Set the {what.upper()}_FIELD environment variable.")


def load_districts() -> gpd.GeoDataFrame:
    """Read the district file, standardise columns, add a centroid for direction maths."""
    if not DISTRICTS_FILE.exists():
        raise SystemExit(f"District file not found: {DISTRICTS_FILE}")
    gdf = gpd.read_file(DISTRICTS_FILE)
    if gdf.crs is None:
        log.warning("District file has no CRS - assuming EPSG:4326.")
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    dcol = _pick_field([c for c in gdf.columns if c != "geometry"], DISTRICT_FIELD, DISTRICT_CANDIDATES, "district")
    pcol = _pick_field([c for c in gdf.columns if c != "geometry"], PROVINCE_FIELD, PROVINCE_CANDIDATES, "province")
    # Names like "Mashonaland_West" -> "Mashonaland West" (matches stakeholder keys)
    out = gpd.GeoDataFrame(
        {"district": gdf[dcol].astype(str).str.replace("_", " ").str.strip(),
         "province": gdf[pcol].astype(str).str.replace("_", " ").str.strip()},
        geometry=gdf.geometry.make_valid(), crs=4326)
    out = out[~out.geometry.is_empty].reset_index(drop=True)
    # A ward-level file has many polygons per district: merge them into one polygon per district.
    # (Faster alternative: run prepare_districts.py once and commit the small GeoJSON it creates.)
    if out.duplicated(["province", "district"]).any():
        n_before = len(out)
        out = out.dissolve(by=["province", "district"], as_index=False)
        out["geometry"] = out.geometry.make_valid()
        log.info("Dissolved %d polygons into %d districts.", n_before, len(out))
    # centroid computed in a metric CRS (UTM 36S covers Zimbabwe well), then back to lat/lon
    cent = gpd.GeoSeries(out.to_crs(32736).geometry.centroid, crs=32736).to_crs(4326)
    out["clat"], out["clon"] = cent.y.values, cent.x.values
    log.info("Loaded %d districts in %d provinces.", len(out), out["province"].nunique())
    return out


# --------------------------------------------------------------------------- #
# FIRMS DOWNLOAD + CLEAN-UP
# --------------------------------------------------------------------------- #
def fetch_source(session, key: str, source: str, bbox: str) -> pd.DataFrame:
    """Download one FIRMS source. Raises on any error (caller decides what to do)."""
    url = FIRMS_URL.format(key=key, source=source, bbox=bbox, days=DAY_RANGE)
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    text = r.text.strip()
    if not text:
        return pd.DataFrame()
    # FIRMS answers errors (bad key, rate limit) as plain text with HTTP 200
    if not text.lower().startswith("latitude"):
        raise RuntimeError(f"FIRMS returned a non-CSV answer: {text[:200]}")
    return pd.read_csv(io.StringIO(text))


def normalise(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Give VIIRS and MODIS rows the same columns."""
    if df.empty:
        return df
    df = df.copy()
    hhmm = df["acq_time"].astype(int).astype(str).str.zfill(4)
    df["time_utc"] = pd.to_datetime(df["acq_date"].astype(str) + " " + hhmm, format="%Y-%m-%d %H%M", utc=True)
    df["source"] = source
    if source == "MODIS_NRT":
        sat = df.get("satellite", pd.Series(["?"] * len(df), index=df.index)).astype(str)
        df["sat_name"] = sat.map({"T": "Terra", "A": "Aqua"}).fillna(sat) + " MODIS"
    else:
        df["sat_name"] = SOURCES[source]

    def rank(v):  # VIIRS: l/n/h ; MODIS: 0-100
        s = str(v).strip().lower()
        if s.isdigit():
            n = int(s)
            return 0 if n < 30 else (1 if n < 80 else 2)
        return {"l": 0, "n": 1, "h": 2}.get(s[:1], 1)

    df["conf_rank"] = df["confidence"].map(rank)
    df["frp"] = pd.to_numeric(df.get("frp"), errors="coerce").fillna(0.0)
    df["pixel_km2"] = pd.to_numeric(df["scan"], errors="coerce") * pd.to_numeric(df["track"], errors="coerce")
    df["pixel_km2"] = df["pixel_km2"].fillna(0.14)
    df["det_id"] = (source + "|" + df["latitude"].round(4).astype(str) + "|"
                    + df["longitude"].round(4).astype(str) + "|" + df["acq_date"].astype(str) + "|" + hhmm)
    return df[["det_id", "source", "sat_name", "latitude", "longitude", "time_utc",
               "conf_rank", "frp", "pixel_km2", "daynight"]]


def locate_in_districts(df: pd.DataFrame, districts: gpd.GeoDataFrame) -> pd.DataFrame:
    """Keep only points that fall inside the real polygons; attach district/province."""
    pts = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["longitude"], df["latitude"]), crs=4326)
    j = gpd.sjoin(pts, districts, how="inner", predicate="intersects")
    j = j[~j.index.duplicated(keep="first")]          # a point on a shared border: keep one
    j["clat"] = districts.loc[j["index_right"], "clat"].values
    j["clon"] = districts.loc[j["index_right"], "clon"].values
    return pd.DataFrame(j.drop(columns=["geometry", "index_right"]))


# --------------------------------------------------------------------------- #
# NEW-FIRE LOGIC
# --------------------------------------------------------------------------- #
def drop_already_seen(df: pd.DataFrame, state: dict):
    """
    Returns (new_df, seen_updates).
      - exact same detection id already alerted  -> ignored
      - within SUPPRESS_RADIUS_KM of an alert from the last SUPPRESS_HOURS -> same fire,
        recorded as seen but NOT alerted again
    """
    alerted = state["alerted"]
    df = df[~df["det_id"].isin(alerted)].copy()
    if df.empty:
        return df, {}
    if SUPPRESS_HOURS > 0 and alerted:
        recent_cut = time.time() - SUPPRESS_HOURS * 3600
        recent = [v for v in alerted.values() if v[2] >= recent_cut]
        if recent:
            ref = np.array(recent)
            d = haversine_matrix(df["latitude"], df["longitude"], ref[:, 0], ref[:, 1])
            same_fire = (d <= SUPPRESS_RADIUS_KM).any(axis=1)
            if same_fire.any():
                log.info("%d detection(s) are continuations of fires alerted in the last %.0f h - suppressed.",
                         same_fire.sum(), SUPPRESS_HOURS)
            suppressed = df[same_fire]
            df = df[~same_fire]
            return df, _as_updates(suppressed)
    return df, {}


def _as_updates(df: pd.DataFrame) -> dict:
    return {r.det_id: [round(r.latitude, 5), round(r.longitude, 5), int(r.time_utc.timestamp())]
            for r in df.itertuples()}


def cluster_labels(lat, lon, radius_km: float) -> np.ndarray:
    """Single-linkage clustering: points within radius_km (directly or via chain) share a label."""
    n = len(lat)
    adj = haversine_matrix(lat, lon, lat, lon) <= radius_km
    labels = -np.ones(n, dtype=int)
    c = 0
    for i in range(n):
        if labels[i] >= 0:
            continue
        labels[i] = c
        stack = [i]
        while stack:
            j = stack.pop()
            for k in np.where(adj[j] & (labels < 0))[0]:
                labels[k] = c
                stack.append(k)
        c += 1
    return labels


def build_events(df: pd.DataFrame) -> list[dict]:
    """Merge neighbouring detections (same district) into fire events."""
    events = []
    for (prov, dist), g in df.groupby(["province", "district"]):
        g = g.reset_index(drop=True)
        g["cl"] = cluster_labels(g["latitude"].values, g["longitude"].values, CLUSTER_RADIUS_KM)
        for _, c in g.groupby("cl"):
            rep = c.loc[c["frp"].idxmax()]                     # strongest pixel = fire position
            # footprint: sum pixel areas per sensor, take the largest (avoids double counting
            # the same ground seen by several satellites). km2 -> hectares
            area_ha = float(c.groupby("source")["pixel_km2"].sum().max() * 100)
            dist_km = float(haversine_matrix([rep.latitude], [rep.longitude], [rep.clat], [rep.clon])[0, 0])
            events.append({
                "province": prov, "district": dist,
                "lat": float(rep.latitude), "lon": float(rep.longitude),
                "time": c["time_utc"].max(),
                "sats": sorted(set(c["sat_name"])),
                "conf": CONF_LABELS[int(c["conf_rank"].max())],
                "frp_max": float(c["frp"].max()), "frp_total": float(c["frp"].sum()),
                "n_det": int(len(c)), "area_ha": area_ha,
                "dist_km": dist_km,
                "bearing": bearing_deg(rep.clat, rep.clon, rep.latitude, rep.longitude),
                "ids": list(c["det_id"]), "wind": None,
            })
    events.sort(key=lambda e: e["frp_max"], reverse=True)
    return events


def get_wind(session, lat: float, lon: float):
    """Current 10 m wind from Open-Meteo (free, no key). Returns (from_deg, km/h) or None."""
    try:
        r = session.get("https://api.open-meteo.com/v1/forecast", timeout=(5, 15), params={
            "latitude": round(lat, 3), "longitude": round(lon, 3),
            "current": "wind_speed_10m,wind_direction_10m", "wind_speed_unit": "kmh"})
        r.raise_for_status()
        cur = r.json()["current"]
        return float(cur["wind_direction_10m"]), float(cur["wind_speed_10m"])
    except Exception as exc:  # wind is a bonus - never block an alert because of it
        log.warning("Wind lookup failed: %s", redact(exc))
        return None


def size_label(frp: float) -> str:
    """Rough intensity class from peak FRP (MW per pixel) - a heuristic, not a measurement."""
    return "small" if frp < 10 else ("moderate" if frp < 50 else "LARGE/intense")


# --------------------------------------------------------------------------- #
# MESSAGE FORMATTING
# --------------------------------------------------------------------------- #
def _wind_text(e: dict, short: bool = False) -> str:
    if not e["wind"]:
        return ""
    wfrom, spd = e["wind"]
    spread = compass((wfrom + 180) % 360)
    if short:
        return f" wind {compass(wfrom)} {spd:.0f}km/h->spreads {spread}"
    return f"💨 Wind from {compass(wfrom)} {spd:.0f} km/h → may spread toward {spread}\n"


def _where(e: dict) -> str:
    if e["dist_km"] < 1.5:
        return f"near {e['district']} district centre"
    return f"{e['dist_km']:.0f} km {compass(e['bearing'])} of {e['district']} centre"


def format_whatsapp(province: str, events: list[dict], now: pd.Timestamp) -> str:
    n_det = sum(e["n_det"] for e in events)
    head = (f"🔥 *FIRE ALERT – {province}*\n{len(events)} new fire(s) ({n_det} satellite detections)\n"
            f"Checked {fmt_time(now)}\n")
    blocks, used = [], len(head)
    for i, e in enumerate(events[:MAX_EVENTS_WHATSAPP], 1):
        b = (f"\n*{i}. {e['district']}* – {size_label(e['frp_max'])}\n"
             f"📍 {e['lat']:.4f}, {e['lon']:.4f}\n"
             f"🕐 {fmt_time(e['time'])}\n"
             f"🛰 {', '.join(e['sats'])} | conf: {e['conf']} | FRP: {e['frp_max']:.1f} MW\n"
             f"📏 {e['n_det']} px ≈ {e['area_ha']:.0f} ha footprint\n"
             f"🧭 {_where(e)}\n"
             f"{_wind_text(e)}"
             f"🗺 {maps_link(e['lat'], e['lon'])}\n")
        if used + len(b) > WHATSAPP_MAX_CHARS and blocks:
            break
        blocks.append(b)
        used += len(b)
    more = len(events) - len(blocks)
    tail = f"\n+{more} more fire(s) not shown." if more > 0 else ""
    return head + "".join(blocks) + tail


def format_sms(province: str, events: list[dict], now: pd.Timestamp) -> str:
    """Plain ASCII, compact - keeps SMS segments (and cost) down."""
    body = f"FIRE ALERT {province}: {len(events)} new fire(s)\n"
    shown = 0
    for i, e in enumerate(events[:MAX_EVENTS_SMS], 1):
        line = (f"{i}) {e['district']} {e['lat']:.4f},{e['lon']:.4f} {fmt_time(e['time'])} "
                f"{e['sats'][0]} {e['conf']} FRP{e['frp_max']:.0f} ~{e['area_ha']:.0f}ha "
                f"{_where(e)}{_wind_text(e, short=True)} {maps_link(e['lat'], e['lon'])}\n")
        if len(body) + len(line) > SMS_MAX_CHARS and shown:
            break
        body += line
        shown += 1
    if len(events) > shown:
        body += f"+{len(events) - shown} more"
    return body.strip()


# --------------------------------------------------------------------------- #
# SENDING
# --------------------------------------------------------------------------- #
def load_stakeholders() -> dict:
    """Secret STAKEHOLDERS_JSON wins (keeps phone numbers out of a public repo)."""
    raw = os.getenv("STAKEHOLDERS_JSON", "").strip()
    try:
        data = json.loads(raw) if raw else json.load(open(STAKEHOLDERS_FILE))
    except FileNotFoundError:
        log.error("No stakeholders configured (secret STAKEHOLDERS_JSON or config/stakeholders.json).")
        return {}
    except json.JSONDecodeError as exc:
        log.error("Stakeholders JSON is invalid: %s", exc)
        return {}
    return {k.strip().lower().replace("_", " "): v for k, v in data.items() if not k.startswith("_")}


def recipients_for(province: str, stakeholders: dict) -> list[dict]:
    people = list(stakeholders.get(province.strip().lower().replace("_", " "), [])) + list(stakeholders.get("*", []))
    seen, out = set(), []
    for p in people:
        if p.get("phone") and p["phone"] not in seen:
            seen.add(p["phone"])
            out.append(p)
    return out


class Notifier:
    def __init__(self, dry_run: bool):
        self.dry = dry_run
        self.client = None
        self.sms_from = os.getenv("TWILIO_SMS_FROM", "")
        self.wa_from = os.getenv("TWILIO_WHATSAPP_FROM", "")
        sid, tok = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
        if not dry_run and sid and tok:
            from twilio.rest import Client
            self.client = Client(sid, tok, timeout=30)
        elif not dry_run:
            log.warning("Twilio credentials missing - SMS/WhatsApp cannot be sent.")

    def send(self, person: dict, wa_text: str, sms_text: str) -> int:
        """Send to one person on their chosen channels. Returns number of messages delivered to Twilio."""
        ok = 0
        for ch in person.get("channels", ["sms"]):
            body, frm, to = (wa_text, "whatsapp:" + self.wa_from.replace("whatsapp:", ""),
                             "whatsapp:" + person["phone"]) if ch == "whatsapp" else (sms_text, self.sms_from, person["phone"])
            if self.dry:
                log.info("[DRY RUN] %s -> %s (%s)\n%s\n", ch, person.get("name", "?"), person["phone"], body)
                ok += 1
                continue
            if not self.client:
                continue
            try:
                m = self.client.messages.create(from_=frm, to=to, body=body)
                log.info("Sent %s to %s (sid %s)", ch, person.get("name", "?"), m.sid)
                ok += 1
            except Exception as exc:
                log.error("Failed %s to %s: %s", ch, person.get("name", "?"), redact(exc))
            time.sleep(0.3)
        return ok

    def telegram(self, session, text: str) -> int:
        """Optional free channel: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."""
        tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not (tok and chat) or self.dry:
            return 0
        try:
            r = session.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                             data={"chat_id": chat, "text": text.replace("*", "")}, timeout=20)
            r.raise_for_status()
            return 1
        except Exception as exc:
            log.error("Telegram failed: %s", redact(exc))
            return 0


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def write_summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


def run(dry_run: bool, baseline: bool) -> int:
    started = pd.Timestamp.now(tz="UTC")
    key = os.getenv("FIRMS_MAP_KEY", "").strip()
    if not key:
        log.error("FIRMS_MAP_KEY is not set.")
        return 1

    districts = load_districts()
    w, s, e, n = districts.total_bounds
    bbox = f"{w - BBOX_BUFFER_DEG:.4f},{s - BBOX_BUFFER_DEG:.4f},{e + BBOX_BUFFER_DEG:.4f},{n + BBOX_BUFFER_DEG:.4f}"
    log.info("Run started %s | bbox %s | days=%d | dry=%s baseline=%s",
             fmt_time(started), bbox, DAY_RANGE, dry_run, baseline)

    session = make_session()
    frames, failed = [], []
    for source in SOURCES:
        try:
            raw = fetch_source(session, key, source, bbox)
            log.info("%-18s %d rows", source, len(raw))
            if not raw.empty:
                frames.append(normalise(raw, source))
        except Exception as exc:                 # one bad source must not stop the others
            failed.append(source)
            log.error("%s failed: %s", source, redact(exc))

    if len(failed) == len(SOURCES):
        log.error("All FIRMS sources failed - nothing checked this run.")
        return 1
    if not frames:
        log.info("No detections in the bounding box. Done.")
        write_summary([f"**{fmt_time(started)}** – no detections. Failed sources: {failed or 'none'}"])
        return 0

    det = pd.concat(frames, ignore_index=True)
    det = det[det["conf_rank"] >= CONF_LABELS.index(MIN_CONFIDENCE)] if MIN_CONFIDENCE in CONF_LABELS else det
    det = locate_in_districts(det, districts) if not det.empty else det
    log.info("%d detections inside district polygons (after confidence filter '%s').", len(det), MIN_CONFIDENCE)
    if det.empty:
        write_summary([f"**{fmt_time(started)}** – no detections inside study area."])
        return 0

    state = load_state()
    original = json.loads(json.dumps(state))
    new, seen_updates = drop_already_seen(det, state)
    state["alerted"].update(seen_updates)

    if baseline:
        state["alerted"].update(_as_updates(new))
        log.info("BASELINE: marked %d detections as seen, no alerts sent.", len(new))
        if not dry_run:
            save_state(state, original)
        return 0

    if new.empty:
        log.info("No NEW fires. Done.")
        if not dry_run:
            save_state(state, original)
        write_summary([f"**{fmt_time(started)}** – no new fires ({len(det)} known detections)."])
        return 0

    events = build_events(new)
    log.info("%d new detections -> %d fire events.", len(new), len(events))

    notifier = Notifier(dry_run)
    stakeholders = load_stakeholders()
    summary = [f"**{fmt_time(started)}** – {len(events)} new fire event(s)"]

    for province in sorted({ev["province"] for ev in events}):
        p_events = [ev for ev in events if ev["province"] == province]
        for ev in p_events[:max(MAX_EVENTS_WHATSAPP, MAX_EVENTS_SMS)]:
            ev["wind"] = get_wind(session, ev["lat"], ev["lon"])
        wa_text = format_whatsapp(province, p_events, started)
        sms_text = format_sms(province, p_events, started)

        people = recipients_for(province, stakeholders)
        delivered = sum(notifier.send(p, wa_text, sms_text) for p in people)
        delivered += notifier.telegram(session, wa_text)
        log.info("%s: %d event(s), %d message(s) delivered to provider, %d recipient(s).",
                 province, len(p_events), delivered, len(people))
        summary.append(f"- {province}: {len(p_events)} event(s), {delivered} message(s) sent")

        # Mark as seen ONLY if at least one message went out; otherwise retry next run.
        if delivered or dry_run:
            for ev in p_events:
                for did in ev["ids"]:
                    r = new.loc[new["det_id"] == did].iloc[0]
                    state["alerted"][did] = [round(r.latitude, 5), round(r.longitude, 5), int(r.time_utc.timestamp())]
        else:
            log.warning("%s: nothing delivered - fires stay 'new' and will be retried.", province)

    if not dry_run:
        save_state(state, original)
    write_summary(summary)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN") == "1")
    ap.add_argument("--baseline", action="store_true", default=os.getenv("BASELINE") == "1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        return run(args.dry_run, args.baseline)
    except SystemExit:
        raise
    except Exception:
        log.exception("Unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
