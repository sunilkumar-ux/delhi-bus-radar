"""
Delhi Transit Nav  -  live tracker for your own route buses (Delhi OTD GTFS-realtime)

Run:        streamlit run app.py
Secrets:    .streamlit/secrets.toml   ->   OTD_API_KEY = "your-key"
            (or env var OTD_API_KEY, or paste it in the sidebar for the current session)
Requires:   streamlit>=1.37  pydeck  pandas  requests  gtfs-realtime-bindings

Architecture
------------
* fetch_feed()   - ONE shared, cached HTTP call (all browser tabs share it, so the
                   API is hit at most once every FETCH_TTL_S seconds).
* FleetTracker   - ONE shared, thread-safe memory of every bus. It is updated from
                   each new feed snapshot exactly once, and it ALWAYS tracks every
                   bus in MY_ROUTES. The route filter only affects what is shown, so
                   filtering can never make buses "vanish" or reset their history.
* live_view()    - a Streamlit fragment that refreshes itself; only the live part of
                   the page re-runs (no blocking sleep(), no full-page flicker).
"""

from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
from collections import deque

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

FEED_URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (DelhiTransitNav)"}

# Internal feed route_id  ->  what you see in the app.
# (Each internal id is already direction-specific, so "dir" is authoritative.)
MY_ROUTES = {
    "3753": {"route": "D-9919", "dir": "Towards Kapashera Border", "color": [220, 20, 20], "badge": "🔴"},
    "3752": {"route": "D-9919", "dir": "Towards Dwarka Mor",       "color": [220, 20, 20], "badge": "🔴"},
    "2804": {"route": "D-068",  "dir": "Towards Sector 21",        "color": [20, 100, 220], "badge": "🔵"},
    "2801": {"route": "D-068",  "dir": "Towards Dwarka Mor",       "color": [20, 100, 220], "badge": "🔵"},
    "2179": {"route": "718",    "dir": "Towards Kapashera Border", "color": [20, 180, 20], "badge": "🟢"},
    "2176": {"route": "718",    "dir": "Towards Uttam Nagar",      "color": [20, 180, 20], "badge": "🟢"},
}
ROUTE_NAMES = sorted({v["route"] for v in MY_ROUTES.values()})

DEFAULT_VIEW = dict(latitude=28.5800, longitude=77.0500, zoom=12.5)
ICON_DATA = {"url": "https://img.icons8.com/color/48/bus.png", "width": 48, "height": 48, "anchorY": 48}

# --- network ---------------------------------------------------------------
FETCH_TTL_S = 4              # shared cache lifetime of one feed download

# --- physics / status thresholds ------------------------------------------
MOVING_KMH = 3.0             # >= this speed counts as "moving"
JITTER_M = 6.0               # GPS moves smaller than this are noise (bus is standing)
TRAIL_MIN_M = 8.0            # min distance between two trail points
MAX_SANE_KMH = 110.0         # faster than this between two fixes = GPS glitch, ignored
MAX_DT_FOR_SPEED_S = 180     # don't derive speed from fixes further apart than this
SPEED_HOLD_S = 60            # no new fix for this long -> speed shown as 0
STALE_S = 180                # no new GPS fix for this long -> "No GPS signal"
STALE_NO_TS_S = 900          # same, but for feeds that carry no GPS timestamps
TRAIL_POINTS = 40

# --- offline detection -----------------------------------------------------
OFFLINE_MISSING_SNAPSHOTS = 3   # must be absent from this many consecutive snapshots ...
OFFLINE_GRACE_S = 45            # ... AND for at least this many seconds
OFFLINE_KEEP_S = 6 * 3600       # forget offline buses after this long
EVENT_LOG_SIZE = 80

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


# =============================================================================
# 2. SMALL HELPERS
# =============================================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def fmt_ist(ts: float | None) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(ts, IST).strftime("%I:%M:%S %p")


def format_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def compass(bearing: float | None) -> str:
    if bearing is None:
        return "-"
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return f"{names[int(((bearing % 360) + 22.5) // 45) % 8]} ({int(bearing) % 360}°)"


def effective_speed(feed_kmh: float | None, calc_kmh: float) -> float:
    """Prefer the speed the bus reports; if it reports ~0 (many feeds always do),
    fall back to the speed derived from consecutive GPS fixes."""
    if feed_kmh is not None and feed_kmh >= 0.5:
        return feed_kmh
    return calc_kmh


def _has(msg, field: str) -> bool:
    try:
        return msg.HasField(field)
    except ValueError:  # field without presence tracking
        return bool(getattr(msg, field, 0))


def fit_view(points: list[tuple[float, float]], fallback: "pdk.ViewState") -> "pdk.ViewState":
    """Centre + zoom so that all (lat, lon) points are visible."""
    if not points:
        return fallback
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat_c = (max(lats) + min(lats)) / 2
    lon_c = (max(lons) + min(lons)) / 2
    lat_span = max(max(lats) - min(lats), 0.004)
    lon_span = max(max(lons) - min(lons), 0.004)
    # deck.gl: visible degrees ~= pixels / 512 * 360 / 2^zoom (conservative pixel sizes)
    zoom = min(math.log2(270.0 / (lat_span * 1.3)), math.log2(420.0 / (lon_span * 1.3)))
    zoom = max(9.5, min(16.0, zoom))
    return pdk.ViewState(latitude=lat_c, longitude=lon_c, zoom=round(zoom, 2), pitch=0, bearing=0)


# =============================================================================
# 3. NETWORK LAYER  (shared session + shared cached download)
# =============================================================================

class FeedError(Exception):
    """Raised with a SAFE message (never contains the API key)."""


@st.cache_resource(show_spinner=False)
def get_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,   # hand us the final response instead of raising
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
    return session


@st.cache_data(ttl=FETCH_TTL_S, show_spinner=False)
def fetch_feed(api_key: str) -> dict:
    """Download + parse the feed. Cached for FETCH_TTL_S seconds across ALL sessions.
    Exceptions are not cached, so a failure is retried on the next call."""
    session = get_http_session()
    try:
        resp = session.get(FEED_URL, params={"key": api_key}, headers=HTTP_HEADERS, timeout=(3.5, 8))
    except requests.exceptions.Timeout:
        raise FeedError("Request timed out") from None
    except requests.exceptions.ConnectionError:
        raise FeedError("Cannot reach otd.delhi.gov.in") from None
    except requests.exceptions.RequestException as exc:   # message could contain the URL/key
        raise FeedError(f"Network error ({type(exc).__name__})") from None

    code = resp.status_code
    if code in (401, 403):
        raise FeedError(f"API key rejected (HTTP {code}) - check OTD_API_KEY")
    if code == 429:
        raise FeedError("Rate-limited by the API (HTTP 429) - increase the refresh interval")
    if code >= 500:
        raise FeedError(f"OTD server error (HTTP {code})")
    if code != 200:
        raise FeedError(f"Unexpected HTTP status {code}")

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(resp.content)
    except Exception:
        raise FeedError("Response is not a valid GTFS-realtime feed") from None
    if len(feed.entity) == 0:
        raise FeedError("Feed contained no vehicles")

    fetched_at = time.time()
    total_vehicles = 0
    max_ts = 0
    found: dict[str, dict] = {}

    for entity in feed.entity:
        if not _has(entity, "vehicle"):
            continue
        veh = entity.vehicle
        total_vehicles += 1
        ts = int(veh.timestamp) if _has(veh, "timestamp") else 0
        max_ts = max(max_ts, ts)

        if not _has(veh, "position"):
            continue
        raw_route = veh.trip.route_id.strip() if _has(veh, "trip") else ""
        if raw_route not in MY_ROUTES:
            continue

        lat, lon = float(veh.position.latitude), float(veh.position.longitude)
        if not (5.0 < lat < 40.0 and 65.0 < lon < 100.0):     # also rejects (0, 0)
            continue

        vid = ""
        if _has(veh, "vehicle"):
            vid = (veh.vehicle.id or veh.vehicle.label or "").strip()
        vid = vid or (entity.id or "").strip() or f"route{raw_route}-{len(found)}"

        rec = {
            "vehicle_id": vid,
            "raw_route": raw_route,
            "lat": lat,
            "lon": lon,
            "ts": ts,
            "speed_ms": float(veh.position.speed) if _has(veh.position, "speed") else None,
            "bearing": float(veh.position.bearing) if _has(veh.position, "bearing") else None,
        }
        old = found.get(vid)
        if old is None or rec["ts"] >= old["ts"]:      # duplicate ids: keep the newest fix
            found[vid] = rec

    # Some feeds encode IST as if it were UTC. Detect an exact +-5h30 skew and correct it,
    # otherwise every bus would look hours old (or "from the future").
    clock_offset = 0
    if max_ts > 0:
        skew = fetched_at - max_ts
        if abs(abs(skew) - 19800) <= 900:
            clock_offset = 19800 if skew > 0 else -19800
    if clock_offset:
        for rec in found.values():
            if rec["ts"] > 0:
                rec["ts"] += clock_offset

    feed_ts = int(feed.header.timestamp) if _has(feed.header, "timestamp") else 0
    if feed_ts:
        feed_ts += clock_offset

    return {
        "fetched_at": fetched_at,
        "feed_ts": feed_ts,
        "clock_offset": clock_offset,
        "total_vehicles": total_vehicles,
        "vehicles": list(found.values()),
    }


# =============================================================================
# 4. FLEET TRACKER  (shared memory + physics)
# =============================================================================

class FleetTracker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.buses: dict[str, dict] = {}
        self.offline: dict[str, dict] = {}
        self.events: deque = deque(maxlen=EVENT_LOG_SIZE)
        self.last_snapshot_at = 0.0
        self.last_ok_wall: float | None = None
        self.last_feed_ts: int | None = None
        self.total_feed_vehicles = 0
        self.clock_offset = 0
        self.snapshots = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_error_wall: float | None = None

    # ---- bookkeeping -------------------------------------------------------
    def _log(self, wall: float, text: str) -> None:
        self.events.appendleft((wall, text))

    def record_error(self, msg: str, wall: float) -> None:
        with self.lock:
            self.failures += 1
            self.last_error = msg
            self.last_error_wall = wall

    # ---- ingest ------------------------------------------------------------
    def update(self, snap: dict) -> bool:
        """Apply one feed snapshot. Idempotent: a snapshot is processed only once,
        no matter how many browser sessions hand it in."""
        with self.lock:
            wall = snap["fetched_at"]
            if wall <= self.last_snapshot_at:
                return False
            self.last_snapshot_at = wall
            self.snapshots += 1
            self.last_ok_wall = wall
            self.last_error = None
            self.last_feed_ts = snap["feed_ts"] or None
            self.total_feed_vehicles = snap["total_vehicles"]
            self.clock_offset = snap["clock_offset"]

            seen = set()
            for rec in snap["vehicles"]:
                seen.add(rec["vehicle_id"])
                self._apply(rec, wall)
            self._sweep(seen, wall)
            return True

    def _apply(self, rec: dict, wall: float) -> None:
        vid = rec["vehicle_id"]
        info = MY_ROUTES[rec["raw_route"]]
        has_ts = rec["ts"] > 0
        returned = self.offline.pop(vid, None)
        st_ = self.buses.get(vid)

        if st_ is None:
            fix_time = rec["ts"] if has_ts else wall
            feed_kmh = rec["speed_ms"] * 3.6 if rec["speed_ms"] is not None else None
            moving = effective_speed(feed_kmh, 0.0) >= MOVING_KMH
            self.buses[vid] = {
                "vehicle_id": vid,
                "route": info["route"], "internal": rec["raw_route"], "direction": info["dir"],
                "badge": info["badge"], "color": info["color"],
                "lat": rec["lat"], "lon": rec["lon"], "bearing": rec["bearing"],
                "fix_time": fix_time, "has_ts": has_ts,
                "first_seen": wall, "last_seen": wall, "missing": 0, "fixes": 1,
                "speed_feed": feed_kmh, "speed_calc": 0.0, "interval": 0.0,
                "stop_since": None if moving else fix_time,
                "stop_exact": False, "seen_moving": moving,
                "trail": deque([(rec["lon"], rec["lat"])], maxlen=TRAIL_POINTS),
            }
            if returned is not None:
                self._log(wall, f"✅ {info['badge']} {info['route']} bus {vid} is back "
                                f"(was offline {format_duration(wall - returned['last_seen'])})")
            return

        # keep descriptive info current (a bus can be reassigned to another route)
        st_.update(route=info["route"], internal=rec["raw_route"], direction=info["dir"],
                   badge=info["badge"], color=info["color"], missing=0, last_seen=wall, has_ts=has_ts)

        moved_m = haversine_m(st_["lat"], st_["lon"], rec["lat"], rec["lon"])
        if has_ts:
            fix_time = rec["ts"]
            if fix_time < st_["fix_time"]:        # out-of-order / older data: ignore
                return
            is_new = fix_time > st_["fix_time"]
        else:                                     # no GPS clock: a changed position is a new fix
            is_new = moved_m > 0.5
            fix_time = wall if is_new else st_["fix_time"]

        if rec["speed_ms"] is not None:
            st_["speed_feed"] = rec["speed_ms"] * 3.6
        if rec["bearing"] is not None:
            st_["bearing"] = rec["bearing"]
        if not is_new:
            return

        prev_fix = st_["fix_time"]
        dt_s = fix_time - prev_fix
        if 0 < dt_s <= 300:     # learn how often THIS bus really reports (smoothed)
            st_["interval"] = dt_s if st_["interval"] <= 0 else 0.7 * st_["interval"] + 0.3 * dt_s

        # speed derived from the two GPS fixes (GPS clock, not our polling clock)
        derived = None
        if moved_m < JITTER_M:
            derived = 0.0
        elif 1.0 <= dt_s <= MAX_DT_FOR_SPEED_S:
            v = moved_m / dt_s * 3.6
            if v <= MAX_SANE_KMH:
                derived = v
        if derived is not None:
            prev = st_["speed_calc"]
            st_["speed_calc"] = derived if (derived == 0.0 or prev <= 0.0) else 0.6 * derived + 0.4 * prev

        if moved_m >= TRAIL_MIN_M:
            st_["trail"].append((rec["lon"], rec["lat"]))
        st_["lat"], st_["lon"] = rec["lat"], rec["lon"]
        st_["fix_time"] = fix_time
        st_["fixes"] += 1

        if effective_speed(st_["speed_feed"], st_["speed_calc"]) >= MOVING_KMH:
            st_["stop_since"] = None
            st_["seen_moving"] = True
        elif st_["stop_since"] is None:
            st_["stop_since"] = prev_fix
            st_["stop_exact"] = st_["seen_moving"]

    def _sweep(self, seen: set, wall: float) -> None:
        for vid in list(self.buses):
            if vid in seen:
                continue
            b = self.buses[vid]
            b["missing"] += 1
            if b["missing"] >= OFFLINE_MISSING_SNAPSHOTS and wall - b["last_seen"] >= OFFLINE_GRACE_S:
                self.offline[vid] = {
                    "vehicle_id": vid, "route": b["route"], "direction": b["direction"],
                    "badge": b["badge"], "color": b["color"], "lat": b["lat"], "lon": b["lon"],
                    "last_seen": b["last_seen"], "last_fix": b["fix_time"],
                }
                del self.buses[vid]
                self._log(wall, f"⚠️ {b['badge']} {b['route']} bus {vid} disappeared from the feed "
                                f"(last seen {fmt_ist(b['last_seen'])})")
        for vid in list(self.offline):
            if wall - self.offline[vid]["last_seen"] > OFFLINE_KEEP_S:
                del self.offline[vid]

    # ---- read side ---------------------------------------------------------
    def get_views(self, now: float) -> list[dict]:
        out = []
        with self.lock:
            for b in self.buses.values():
                age = max(0.0, now - b["fix_time"])
                iv = b["interval"]
                stale = age > max(STALE_S if b["has_ts"] else STALE_NO_TS_S, 4 * iv)
                speed = effective_speed(b["speed_feed"], b["speed_calc"])
                if age > max(SPEED_HOLD_S, 2.5 * iv):
                    speed = 0.0

                if stale:
                    state, label = "stale", "No GPS"
                    status = f"📡 No GPS signal (last fix {format_duration(age)} ago)"
                elif speed >= MOVING_KMH:
                    state, label, status = "moving", "Moving", "🟢 Moving"
                else:
                    since = b["stop_since"] if b["stop_since"] is not None else b["fix_time"]
                    exact = b["stop_since"] is not None and b["stop_exact"]
                    state, label = "stopped", "Stopped"
                    status = f"🛑 Stopped ({'' if exact else '≥ '}{format_duration(now - since)})"

                ring = [140, 140, 140, 170] if stale else list(b["color"]) + [170]
                out.append({
                    "vehicle_id": b["vehicle_id"], "route": b["route"], "internal_id": b["internal"],
                    "direction": b["direction"], "badge": b["badge"], "color": list(b["color"]),
                    "ring_color": ring, "lat": b["lat"], "lon": b["lon"],
                    "speed_kmh": round(speed, 1), "state": state, "state_label": label, "status": status,
                    "bearing": b["bearing"], "heading": compass(b["bearing"]),
                    "age_s": age, "age_text": f"{format_duration(age)} ago",
                    "last_fix_ist": fmt_ist(b["fix_time"]), "icon_data": ICON_DATA,
                })
        return out

    def get_trail(self, vid: str) -> list[list[float]]:
        with self.lock:
            b = self.buses.get(vid)
            return [list(p) for p in b["trail"]] if b else []

    def get_offline(self, now: float) -> list[dict]:
        with self.lock:
            return [dict(o, offline_for=now - o["last_seen"]) for o in self.offline.values()]

    def get_events(self) -> list[tuple[float, str]]:
        with self.lock:
            return list(self.events)

    def get_health(self) -> dict:
        with self.lock:
            return {
                "last_ok_wall": self.last_ok_wall, "last_feed_ts": self.last_feed_ts,
                "total_feed_vehicles": self.total_feed_vehicles, "clock_offset": self.clock_offset,
                "snapshots": self.snapshots, "failures": self.failures,
                "last_error": self.last_error, "last_error_wall": self.last_error_wall,
                "tracked": len(self.buses), "offline": len(self.offline),
            }


@st.cache_resource(show_spinner=False)
def get_tracker() -> FleetTracker:
    return FleetTracker()


# =============================================================================
# 5. UI
# =============================================================================

def resolve_api_key() -> tuple[str | None, str]:
    """secrets.toml -> environment variable -> sidebar field (session only)."""
    try:
        key = st.secrets.get("OTD_API_KEY")
        if key:
            return str(key), "secrets"
    except Exception:
        pass
    key = os.environ.get("OTD_API_KEY")
    if key:
        return key, "environment"
    typed = st.sidebar.text_input("🔑 OTD API key", type="password",
                                  help="Kept only in this browser session. Better: set OTD_API_KEY in secrets.")
    return (typed.strip() or None), "sidebar"


def rerun_fragment() -> None:
    try:
        st.rerun(scope="fragment")
    except Exception:        # old Streamlit without scope=, or called outside a fragment run
        st.rerun()


def main() -> None:
    st.set_page_config(layout="wide", page_title="Delhi Transit Nav", page_icon="🚍")
    st.title("🚍 Advanced Transit Navigation")

    if "view_state" not in st.session_state:
        st.session_state.view_state = pdk.ViewState(pitch=0, bearing=0, **DEFAULT_VIEW)
    if "target_bus" not in st.session_state:
        st.session_state.target_bus = "None"

    # ---------------- sidebar ----------------
    sb = st.sidebar
    sb.header("🎯 Map Settings")
    theme_code = "dark" if "Dark" in sb.radio("Map appearance:", ["Dark Mode (Night)", "Light Mode (Day)"]) else "light"
    selected_route = sb.radio("Filter route:", ["All Buses"] + ROUTE_NAMES)
    lock_camera = sb.toggle("🔒 Strict tracking lock", value=True,
                            help="With a bus selected: camera follows it and map panning is disabled.")
    auto_fit = sb.toggle("📐 Auto-fit map to buses", value=True,
                         help="When no bus is selected, zoom the map so all visible buses fit.")
    zoom_level = sb.slider("Follow zoom", 11.0, 18.0, 15.0, 0.5, help="Zoom used while following a bus.")
    show_trail = sb.toggle("🧵 Show trail of tracked bus", value=True)
    show_offline_pins = sb.toggle("👻 Show last position of offline buses", value=False)

    sb.divider()
    sb.header("⏱️ Refresh Controls")
    auto_refresh = sb.toggle("Enable auto-refresh", value=True)
    refresh_rate = int(sb.number_input("Refresh every (seconds):", min_value=2, max_value=60, value=5))
    if sb.button("🔄 Refresh now"):
        st.rerun()

    sb.divider()
    sb.header("📍 My stop (optional)")
    use_my_stop = sb.toggle("Show distance to my stop", value=False)
    my_lat = sb.number_input("Latitude", value=28.6190, format="%.5f", disabled=not use_my_stop)
    my_lon = sb.number_input("Longitude", value=77.0321, format="%.5f", disabled=not use_my_stop)

    sb.divider()
    api_key, key_source = resolve_api_key()
    if not api_key:
        st.error("No API key found. Add `OTD_API_KEY = \"...\"` to `.streamlit/secrets.toml` "
                 "(or Streamlit Cloud → App settings → Secrets), or paste it in the sidebar.")
        st.stop()

    tracker = get_tracker()

    # ---------------- live fragment ----------------
    def live_view() -> None:
        # 1) data: shared cached download -> shared tracker
        error_msg = None
        snapshot = None
        try:
            snapshot = fetch_feed(api_key)
        except FeedError as exc:
            error_msg = str(exc)
        except Exception as exc:                       # never leak details / the key
            error_msg = f"Unexpected error ({type(exc).__name__})"
        if snapshot is not None:
            tracker.update(snapshot)
        else:
            tracker.record_error(error_msg or "Unknown error", time.time())

        now = time.time()
        health = tracker.get_health()
        all_views = tracker.get_views(now)
        views = [v for v in all_views if selected_route in ("All Buses", v["route"])]
        views.sort(key=lambda v: (v["route"], v["direction"], v["vehicle_id"]))
        offline = [o for o in tracker.get_offline(now) if selected_route in ("All Buses", o["route"])]
        offline.sort(key=lambda o: o["last_seen"], reverse=True)

        if use_my_stop:
            for v in views:
                v["dist_km"] = round(haversine_m(v["lat"], v["lon"], my_lat, my_lon) / 1000, 2)

        # 2) connection banner
        if health["last_error"]:
            since = (f"Showing the last known data from {format_duration(now - health['last_ok_wall'])} ago."
                     if health["last_ok_wall"] else "No data received yet.")
            st.warning(f"⚠️ Live feed problem: {health['last_error']}. {since} Retrying automatically…")
        elif health["last_ok_wall"] is None:
            st.info("Connecting to the feed…")

        # 3) summary metrics
        n_move = sum(v["state"] == "moving" for v in views)
        n_stop = sum(v["state"] == "stopped" for v in views)
        n_stale = sum(v["state"] == "stale" for v in views)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("🚍 Active", len(views))
        m2.metric("🟢 Moving", n_move)
        m3.metric("🛑 Stopped", n_stop)
        m4.metric("📡 No GPS", n_stale)
        m5.metric("🔴 Offline", len(offline))
        if health["last_ok_wall"]:
            feed_age = (f" · feed generated {format_duration(now - health['last_feed_ts'])} ago"
                        if health["last_feed_ts"] else "")
            st.caption(f"Updated {fmt_ist(health['last_ok_wall'])} IST{feed_age} · "
                       f"{health['total_feed_vehicles']} vehicles in feed · {health['tracked']} of yours tracked")

        # 4) target selection
        target_id = st.session_state.target_bus
        target_info = next((v for v in views if v["vehicle_id"] == target_id), None) if target_id != "None" else None

        col_target, _ = st.columns([3, 1])
        with col_target:
            if target_id != "None" and target_info is None:
                st.warning(f"🎯 Target bus **{target_id}** is not in the live data right now (offline or hidden by the "
                           "route filter). Tracking resumes automatically if it returns.")
                if st.button("Clear target"):
                    st.session_state.target_bus = "None"
                    rerun_fragment()

            options = ["None"] + [v["vehicle_id"] for v in views]
            labels = {"None": "🚫 Free roam (do not track any bus)"}
            for v in views:
                labels[v["vehicle_id"]] = f"{v['badge']} {v['route']}  |  🏁 {v['direction']}  |  {v['vehicle_id']}"
            idx = options.index(target_id) if target_id in options else 0
            title = (f"🎯 Tracking: {target_info['route']} ➔ {target_info['direction']} ({target_info['vehicle_id']})"
                     if target_info else "🎯 Target & centre camera on a specific bus")
            with st.expander(title, expanded=target_info is None):
                choice = st.radio("Select bus to track:", options, index=idx,
                                  format_func=lambda x: labels.get(x, x), label_visibility="collapsed")
            if choice != options[idx]:                  # user picked something new
                st.session_state.target_bus = choice
                rerun_fragment()

        if target_info:
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Speed", f"{target_info['speed_kmh']:.1f} km/h")
            d2.metric("Status", target_info["state_label"])
            d3.metric("Heading", target_info["heading"])
            d4.metric("Last GPS fix", target_info["age_text"])
            extra = f" · {target_info['dist_km']} km from your stop (straight line)" if use_my_stop else ""
            st.caption(f"{target_info['status']} · fix at {target_info['last_fix_ist']} IST{extra}")

        # 5) camera
        if target_info and lock_camera:
            st.session_state.view_state = pdk.ViewState(
                latitude=target_info["lat"], longitude=target_info["lon"], zoom=zoom_level, pitch=0, bearing=0)
        elif not target_info and auto_fit and views:
            st.session_state.view_state = fit_view([(v["lat"], v["lon"]) for v in views],
                                                   st.session_state.view_state)

        # 6) map + tables
        col_map, col_tables = st.columns([2.5, 1.5])

        with col_map:
            layers = []
            text_color = [0, 0, 0, 255] if theme_code == "light" else [255, 255, 255, 255]

            if show_offline_pins and offline:
                df_off_map = pd.DataFrame([{"lat": o["lat"], "lon": o["lon"]} for o in offline])
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=df_off_map, get_position="[lon, lat]",
                    get_fill_color=[150, 150, 150, 110], get_radius=10, radius_units="pixels"))

            if show_trail and target_info:
                trail = tracker.get_trail(target_info["vehicle_id"])
                if len(trail) >= 2:
                    layers.append(pdk.Layer(
                        "PathLayer", data=[{"path": trail}], get_path="path",
                        get_color=[255, 215, 0, 220], get_width=4, width_units="pixels"))

            if views:
                df = pd.DataFrame(views)[[
                    "route", "vehicle_id", "direction", "speed_kmh", "status", "last_fix_ist",
                    "lat", "lon", "ring_color", "icon_data"]]
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=df, get_position="[lon, lat]", get_fill_color="ring_color",
                    get_radius=17, radius_units="pixels"))
                layers.append(pdk.Layer(
                    "IconLayer", data=df, get_icon="icon_data", get_size=4, size_scale=10,
                    get_position="[lon, lat]", pickable=True,
                    transitions={"getPosition": {"duration": 1000}}))
                layers.append(pdk.Layer(
                    "TextLayer", data=df, get_position="[lon, lat]", get_text="route", get_size=16,
                    get_color=text_color, get_pixel_offset="[0, 28]",
                    transitions={"getPosition": {"duration": 1000}}))

            if target_info:
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=pd.DataFrame([{"lat": target_info["lat"], "lon": target_info["lon"]}]),
                    get_position="[lon, lat]", get_fill_color=[255, 255, 0, 60], get_line_color=[255, 255, 0, 255],
                    get_radius=30, radius_units="pixels", stroked=True, line_width_min_pixels=3))

            if use_my_stop:
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=pd.DataFrame([{"lat": my_lat, "lon": my_lon}]),
                    get_position="[lon, lat]", get_fill_color=[0, 160, 255, 230], get_line_color=[255, 255, 255, 255],
                    get_radius=9, radius_units="pixels", stroked=True, line_width_min_pixels=2))

            if target_info and lock_camera:
                map_view = pdk.View(type="MapView", controller={"dragPan": False, "scrollZoom": True, "touchZoom": True})
            else:
                map_view = pdk.View(type="MapView", controller=True)

            st.pydeck_chart(pdk.Deck(
                layers=layers,
                initial_view_state=st.session_state.view_state,
                views=[map_view],
                map_style=theme_code,
                tooltip={
                    "html": "<b>{route}</b> · {vehicle_id}<br/>{direction}<br/>"
                            "Speed: {speed_kmh} km/h<br/>{status}<br/>Last fix: {last_fix_ist}",
                    "style": {"backgroundColor": "#1f1f1f", "color": "white", "fontSize": "13px"},
                },
            ), height=520)

        with col_tables:
            st.subheader("🟢 Live Active Buses")
            if views:
                cols = ["route", "vehicle_id", "direction", "speed_kmh", "status", "age_text"]
                names = {"route": "Route", "vehicle_id": "Vehicle ID", "direction": "Direction",
                         "speed_kmh": "Speed (km/h)", "status": "Status", "age_text": "Last GPS fix"}
                if use_my_stop:
                    cols.append("dist_km")
                    names["dist_km"] = "To my stop (km)"
                st.dataframe(pd.DataFrame(views)[cols].rename(columns=names), hide_index=True,
                             column_config={"Speed (km/h)": st.column_config.NumberColumn(format="%.1f")})
            else:
                st.warning("No active buses right now.")

            st.markdown("---")
            st.subheader("🔴 Disappeared / Offline Buses")
            if offline:
                df_off = pd.DataFrame([{
                    "Route": o["route"], "Vehicle ID": o["vehicle_id"], "Direction": o["direction"],
                    "Last seen (IST)": fmt_ist(o["last_seen"]), "Offline for": format_duration(o["offline_for"]),
                } for o in offline])
                st.dataframe(df_off, hide_index=True)
            else:
                st.info("No buses have disappeared since the server started tracking.")

        # 7) event log + diagnostics
        with st.expander("📜 Event log"):
            events = tracker.get_events()
            if events:
                for wall, text in events[:30]:
                    st.write(f"`{fmt_ist(wall)}`  {text}")
            else:
                st.caption("Nothing yet.")
        with st.expander("🛠️ Diagnostics"):
            st.write({
                "API key source": key_source,
                "Snapshots processed": health["snapshots"],
                "Failed fetches": health["failures"],
                "Last error": health["last_error"] or "none",
                "Feed clock correction (s)": health["clock_offset"],
                "Buses tracked / offline": f"{health['tracked']} / {health['offline']}",
            })
            st.caption("Tracking runs only while at least one browser tab has this app open "
                       "(Streamlit has no background worker), and resets when the server restarts.")

    # ---------------- run it (self-refreshing fragment) ----------------
    fragment = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
    if fragment is not None:
        fragment(run_every=refresh_rate if auto_refresh else None)(live_view)()
    else:                                              # very old Streamlit fallback
        live_view()
        if auto_refresh:
            time.sleep(refresh_rate)
            st.rerun()


if __name__ == "__main__":
    main()
