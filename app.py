"""
Delhi Transit Radar - Live Bus Fleet Tracking
Real-time tracking of Delhi public transit buses (Delhi OTD GTFS-realtime).
Optimized for mobile & desktop with rotatable directional bus icons and high-contrast neon radar styling.
"""

from __future__ import annotations

import base64
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
# 1. CONFIGURATION & NEON THEME PALETTE
# =============================================================================

FEED_URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (DelhiTransitRadar)"}

# High-contrast neon color mapping per route:
# 718    -> Electric Neon Green (#00FF66)
# D-068  -> Cyber Neon Blue   (#00E5FF)
# D-9919 -> Vivid Neon Red    (#FF2A6D)
MY_ROUTES = {
    "3753": {
        "route": "D-9919",
        "short": "9919",
        "dir": "Towards Kapashera Border",
        "color": [255, 42, 109],
        "neon": "#FF2A6D",
        "neon_bg": "rgba(255, 42, 109, 0.2)",
        "badge": "🔴",
    },
    "3752": {
        "route": "D-9919",
        "short": "9919",
        "dir": "Towards Dwarka Mor",
        "color": [255, 42, 109],
        "neon": "#FF2A6D",
        "neon_bg": "rgba(255, 42, 109, 0.2)",
        "badge": "🔴",
    },
    "2804": {
        "route": "D-068",
        "short": "068",
        "dir": "Towards Sector 21",
        "color": [0, 229, 255],
        "neon": "#00E5FF",
        "neon_bg": "rgba(0, 229, 255, 0.2)",
        "badge": "🔵",
    },
    "2801": {
        "route": "D-068",
        "short": "068",
        "dir": "Towards Dwarka Mor",
        "color": [0, 229, 255],
        "neon": "#00E5FF",
        "neon_bg": "rgba(0, 229, 255, 0.2)",
        "badge": "🔵",
    },
    "2179": {
        "route": "718",
        "short": "718",
        "dir": "Towards Kapashera Border",
        "color": [0, 255, 102],
        "neon": "#00FF66",
        "neon_bg": "rgba(0, 255, 102, 0.2)",
        "badge": "🟢",
    },
    "2176": {
        "route": "718",
        "short": "718",
        "dir": "Towards Uttam Nagar",
        "color": [0, 255, 102],
        "neon": "#00FF66",
        "neon_bg": "rgba(0, 255, 102, 0.2)",
        "badge": "🟢",
    },
}
ROUTE_NAMES = sorted({v["route"] for v in MY_ROUTES.values()})

# Default map view focused on South-West Delhi corridor
DEFAULT_VIEW = dict(latitude=28.5800, longitude=77.0500, zoom=12.4)

# Free public CARTO vector basemap styles (Zero Mapbox token required)
CARTO_DARK = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
CARTO_LIGHT = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"

# Physics / telemetry thresholds
MOVING_KMH = 3.0
JITTER_M = 5.0
TRAIL_MIN_M = 15.0
TRAIL_MAX_GAP_M = 350.0  # Reset trail if jump exceeds 350m to avoid chords cutting across city
MAX_SANE_KMH = 105.0
MAX_DT_FOR_SPEED_S = 180
SPEED_HOLD_S = 60
STALE_S = 180
STALE_NO_TS_S = 900
TRAIL_POINTS = 35

# Network caching & timing
FETCH_TTL_S = 3  # Fast 3s cache for immediate location synchronization
OFFLINE_MISSING_SNAPSHOTS = 3
OFFLINE_GRACE_S = 45
OFFLINE_KEEP_S = 4 * 3600

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


# =============================================================================
# 2. DIRECTIONAL SVG BUS ICON GENERATOR
# =============================================================================

def generate_bus_svg_icon(color_hex: str) -> dict:
    """
    Generates a top-down transit bus SVG icon pointing North (0 degrees).
    When Deck.gl applies get_angle="bearing", the bus icon rotates smoothly
    to face the exact direction of travel (North, South, East, West, etc.).
    """
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60" viewBox="0 0 60 60">
  <rect x="14" y="6" width="32" height="48" rx="8" fill="{color_hex}" fill-opacity="0.25"/>
  <rect x="16" y="8" width="28" height="44" rx="7" fill="#0b1120" stroke="{color_hex}" stroke-width="2.5"/>
  <circle cx="19.5" cy="10" r="2.2" fill="#ffffff"/>
  <circle cx="40.5" cy="10" r="2.2" fill="#ffffff"/>
  <path d="M20 13.5 Q30 12 40 13.5 L39.5 20.5 Q30 21.5 20.5 20.5 Z" fill="{color_hex}" fill-opacity="0.95"/>
  <polygon points="30,24 24,34 30,31.5 36,34" fill="{color_hex}"/>
  <rect x="17" y="24" width="2.5" height="5.5" rx="1" fill="#64748b"/>
  <rect x="40.5" y="24" width="2.5" height="5.5" rx="1" fill="#64748b"/>
  <rect x="17" y="33" width="2.5" height="5.5" rx="1" fill="#64748b"/>
  <rect x="40.5" y="33" width="2.5" height="5.5" rx="1" fill="#64748b"/>
  <rect x="18" y="50" width="4.5" height="2" rx="1" fill="#ef4444"/>
  <rect x="37.5" y="50" width="4.5" height="2" rx="1" fill="#ef4444"/>
</svg>"""
    b64 = base64.b64encode(svg.encode("utf-8")).decode("utf-8")
    return {
        "url": f"data:image/svg+xml;base64,{b64}",
        "width": 60,
        "height": 60,
        "anchorX": 30,
        "anchorY": 30,
    }

# Pre-generate self-contained vector icons for zero network latency & no CORS
BUS_ICONS = {
    "#00FF66": generate_bus_svg_icon("#00FF66"),  # Route 718 (Neon Green)
    "#00E5FF": generate_bus_svg_icon("#00E5FF"),  # Route D-068 (Neon Blue)
    "#FF2A6D": generate_bus_svg_icon("#FF2A6D"),  # Route D-9919 (Neon Red)
}


# =============================================================================
# 3. HELPER FUNCTIONS & GEOMETRY
# =============================================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def calc_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes the forward azimuth compass heading in degrees from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    initial_bearing = math.degrees(math.atan2(y, x))
    return (initial_bearing + 360.0) % 360.0


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


def effective_speed(feed_kmh: float | None, calc_kmh: float) -> float:
    if feed_kmh is not None and feed_kmh >= 0.5:
        return feed_kmh
    return calc_kmh


def _has(msg, field: str) -> bool:
    try:
        return msg.HasField(field)
    except ValueError:
        return bool(getattr(msg, field, 0))


def fit_view(points: list[tuple[float, float]], fallback: pdk.ViewState) -> pdk.ViewState:
    """Safe bounding box fitting restricted to sensible Delhi NCR coordinates."""
    valid = [p for p in points if 28.2 < p[0] < 29.1 and 76.6 < p[1] < 77.6]
    if not valid:
        return fallback
    lats = [p[0] for p in valid]
    lons = [p[1] for p in valid]
    lat_c = (max(lats) + min(lats)) / 2
    lon_c = (max(lons) + min(lons)) / 2
    lat_span = max(max(lats) - min(lats), 0.012)
    lon_span = max(max(lons) - min(lons), 0.012)

    zoom_lat = math.log2(280.0 / (lat_span * 1.35))
    zoom_lon = math.log2(420.0 / (lon_span * 1.35))
    zoom = min(zoom_lat, zoom_lon)
    zoom = max(11.0, min(15.2, zoom))
    return pdk.ViewState(latitude=lat_c, longitude=lon_c, zoom=round(zoom, 2), pitch=0, bearing=0)


# =============================================================================
# 4. NETWORK INGESTION LAYER
# =============================================================================

class FeedError(Exception):
    pass


@st.cache_resource(show_spinner=False)
def get_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
    return session


@st.cache_data(ttl=FETCH_TTL_S, show_spinner=False)
def fetch_feed(api_key: str) -> dict:
    session = get_http_session()
    try:
        resp = session.get(FEED_URL, params={"key": api_key}, headers=HTTP_HEADERS, timeout=(3.5, 7.5))
    except requests.exceptions.Timeout:
        raise FeedError("Request timed out - OTD server unresponsive") from None
    except requests.exceptions.ConnectionError:
        raise FeedError("Cannot reach otd.delhi.gov.in") from None
    except requests.exceptions.RequestException as exc:
        raise FeedError(f"Network error ({type(exc).__name__})") from None

    code = resp.status_code
    if code in (401, 403):
        raise FeedError(f"API key rejected (HTTP {code}) - verify OTD_API_KEY")
    if code == 429:
        raise FeedError("Rate-limited (HTTP 429) - refresh interval too short")
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
        raise FeedError("Feed contains 0 vehicles right now")

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
        if not (28.0 < lat < 29.5 and 76.5 < lon < 78.0):
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
        if old is None or rec["ts"] >= old["ts"]:
            found[vid] = rec

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
        "total_vehicles": total_vehicles,
        "vehicles": list(found.values()),
    }


# =============================================================================
# 5. FLEET TRACKER & TELEMETRY ENGINE
# =============================================================================

class FleetTracker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.buses: dict[str, dict] = {}
        self.offline: dict[str, dict] = {}
        self.last_snapshot_at = 0.0
        self.last_ok_wall: float | None = None
        self.last_feed_ts: int | None = None
        self.total_feed_vehicles = 0
        self.last_error: str | None = None

    def record_error(self, msg: str) -> None:
        with self.lock:
            self.last_error = msg

    def update(self, snap: dict) -> bool:
        with self.lock:
            wall = snap["fetched_at"]
            if wall <= self.last_snapshot_at:
                return False
            self.last_snapshot_at = wall
            self.last_ok_wall = wall
            self.last_error = None
            self.last_feed_ts = snap["feed_ts"] or None
            self.total_feed_vehicles = snap["total_vehicles"]

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
        self.offline.pop(vid, None)
        st_ = self.buses.get(vid)

        bearing = rec["bearing"] if rec["bearing"] is not None and rec["bearing"] > 0 else 0.0

        if st_ is None:
            fix_time = rec["ts"] if has_ts else wall
            feed_kmh = rec["speed_ms"] * 3.6 if rec["speed_ms"] is not None else None
            moving = effective_speed(feed_kmh, 0.0) >= MOVING_KMH
            self.buses[vid] = {
                "vehicle_id": vid,
                "route": info["route"],
                "short": info["short"],
                "direction": info["dir"],
                "badge": info["badge"],
                "neon": info["neon"],
                "neon_bg": info["neon_bg"],
                "color": info["color"],
                "lat": rec["lat"],
                "lon": rec["lon"],
                "bearing": bearing,
                "fix_time": fix_time,
                "has_ts": has_ts,
                "last_seen": wall,
                "missing": 0,
                "speed_feed": feed_kmh,
                "speed_calc": 0.0,
                "interval": 0.0,
                "stop_since": None if moving else fix_time,
                "seen_moving": moving,
                "trail": deque([(rec["lon"], rec["lat"])], maxlen=TRAIL_POINTS),
            }
            return

        st_.update(
            route=info["route"],
            short=info["short"],
            direction=info["dir"],
            badge=info["badge"],
            neon=info["neon"],
            neon_bg=info["neon_bg"],
            color=info["color"],
            missing=0,
            last_seen=wall,
            has_ts=has_ts,
        )

        moved_m = haversine_m(st_["lat"], st_["lon"], rec["lat"], rec["lon"])
        if has_ts:
            fix_time = rec["ts"]
            if fix_time < st_["fix_time"]:
                return
            is_new = fix_time > st_["fix_time"]
        else:
            is_new = moved_m > 0.5
            fix_time = wall if is_new else st_["fix_time"]

        # Calculate accurate heading if bus moved
        if rec["bearing"] is not None and rec["bearing"] > 0:
            st_["bearing"] = rec["bearing"]
        elif moved_m >= JITTER_M:
            st_["bearing"] = calc_bearing(st_["lat"], st_["lon"], rec["lat"], rec["lon"])

        if rec["speed_ms"] is not None:
            st_["speed_feed"] = rec["speed_ms"] * 3.6
        if not is_new:
            return

        dt_s = fix_time - st_["fix_time"]
        if 0 < dt_s <= 300:
            st_["interval"] = dt_s if st_["interval"] <= 0 else 0.7 * st_["interval"] + 0.3 * dt_s

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

        # Trail filtering: only append real travel, reset if teleport / jump > 350m
        if moved_m >= TRAIL_MIN_M:
            if moved_m > TRAIL_MAX_GAP_M:
                st_["trail"].clear()
            st_["trail"].append((rec["lon"], rec["lat"]))

        st_["lat"], st_["lon"] = rec["lat"], rec["lon"]
        st_["fix_time"] = fix_time

        if effective_speed(st_["speed_feed"], st_["speed_calc"]) >= MOVING_KMH:
            st_["stop_since"] = None
            st_["seen_moving"] = True
        elif st_["stop_since"] is None:
            st_["stop_since"] = fix_time

    def _sweep(self, seen: set, wall: float) -> None:
        for vid in list(self.buses):
            if vid in seen:
                continue
            b = self.buses[vid]
            b["missing"] += 1
            if b["missing"] >= OFFLINE_MISSING_SNAPSHOTS and wall - b["last_seen"] >= OFFLINE_GRACE_S:
                self.offline[vid] = {
                    "vehicle_id": vid,
                    "route": b["route"],
                    "short": b["short"],
                    "direction": b["direction"],
                    "badge": b["badge"],
                    "neon": b["neon"],
                    "neon_bg": b["neon_bg"],
                    "lat": b["lat"],
                    "lon": b["lon"],
                    "last_seen": b["last_seen"],
                    "last_fix": b["fix_time"],
                }
                del self.buses[vid]
        for vid in list(self.offline):
            if wall - self.offline[vid]["last_seen"] > OFFLINE_KEEP_S:
                del self.offline[vid]

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
                    state = "stale"
                    status_short = "📡 No GPS"
                    status_text = "📡 Stale GPS Fix"
                elif speed >= MOVING_KMH:
                    state = "moving"
                    status_short = f"🟢 {speed:.1f} km/h"
                    status_text = f"🟢 Moving ({speed:.1f} km/h)"
                else:
                    since = b["stop_since"] if b["stop_since"] is not None else b["fix_time"]
                    stop_dur = format_duration(now - since)
                    state = "stopped"
                    status_short = f"🛑 Stopped ({stop_dur})"
                    status_text = f"🛑 Stopped for {stop_dur}"

                icon_spec = BUS_ICONS.get(b["neon"], BUS_ICONS["#00E5FF"])

                out.append({
                    "vehicle_id": b["vehicle_id"],
                    "route": b["route"],
                    "route_short": b["short"],
                    "direction": b["direction"],
                    "badge": b["badge"],
                    "neon": b["neon"],
                    "neon_bg": b["neon_bg"],
                    "color": list(b["color"]),
                    "lat": b["lat"],
                    "lon": b["lon"],
                    "speed_kmh": round(speed, 1),
                    "state": state,
                    "status_short": status_short,
                    "status_text": status_text,
                    "bearing": b["bearing"],
                    "bearing_angle": float(b.get("bearing", 0.0) or 0.0),
                    "icon_spec": icon_spec,
                    "age_s": age,
                    "age_text": f"{format_duration(age)} ago",
                    "last_fix_ist": fmt_ist(b["fix_time"]),
                })
        return out

    def get_trail(self, vid: str) -> list[list[float]]:
        with self.lock:
            b = self.buses.get(vid)
            return [list(p) for p in b["trail"]] if b else []

    def get_health(self) -> dict:
        with self.lock:
            return {
                "last_ok_wall": self.last_ok_wall,
                "last_feed_ts": self.last_feed_ts,
                "total_feed_vehicles": self.total_feed_vehicles,
                "last_error": self.last_error,
                "tracked": len(self.buses),
                "offline": len(self.offline),
            }


@st.cache_resource(show_spinner=False)
def get_tracker() -> FleetTracker:
    return FleetTracker()


# =============================================================================
# 6. UI & MOBILE STYLING
# =============================================================================

CUSTOM_CSS = """
<style>
/* Clean dark transit theme */
.stApp {
    background-color: #080d1a;
    color: #f1f5f9;
}
header[data-testid="stHeader"] {
    background-color: transparent !important;
}

/* App Header */
.app-header-strip {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 2px 0 6px 0;
    margin-bottom: 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.app-brand-title {
    font-size: 1.25rem;
    font-weight: 800;
    letter-spacing: -0.5px;
    background: linear-gradient(90deg, #00E5FF, #00FF66);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}
.app-feed-meta {
    font-size: 0.72rem;
    color: #94a3b8;
    background: rgba(255, 255, 255, 0.05);
    padding: 3px 8px;
    border-radius: 12px;
}

/* Compact 3-Pill Stats Bar (Active, Moving, Stopped) */
.stats-strip {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin: 4px 0 8px 0;
}
.stat-pill {
    flex: 1;
    background: rgba(18, 26, 43, 0.9);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 6px 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
}
.stat-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
}
.dot-active { background: #00E5FF; box-shadow: 0 0 6px #00E5FF; }
.dot-moving { background: #00FF66; box-shadow: 0 0 6px #00FF66; }
.dot-stopped { background: #FFAB00; box-shadow: 0 0 6px #FFAB00; }
.stat-val {
    font-size: 1.05rem;
    font-weight: 800;
    line-height: 1;
}
.stat-lbl {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    color: #94a3b8;
}
.stat-active .stat-val { color: #00E5FF; }
.stat-moving .stat-val { color: #00FF66; }
.stat-stopped .stat-val { color: #FFAB00; }

/* Compact Tracking HUD Card */
.hud-strip {
    background: linear-gradient(135deg, rgba(15, 23, 42, 0.95), rgba(24, 33, 53, 0.9));
    border: 1px solid rgba(0, 229, 255, 0.35);
    border-radius: 10px;
    padding: 8px 12px;
    margin: 6px 0 10px 0;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}
.hud-top-line {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
}
.route-badge {
    padding: 2px 8px;
    border-radius: 5px;
    font-weight: 800;
    font-size: 0.82rem;
    letter-spacing: 0.5px;
}
.plate-num {
    font-family: monospace;
    font-size: 1rem;
    font-weight: 800;
    color: #ffffff;
}
.dir-sub {
    font-size: 0.8rem;
    color: #94a3b8;
}
.hud-metrics-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 0.85rem;
    font-weight: 700;
}
.speed-tag { color: #00E5FF; }

/* High-Density Fleet Row */
.fleet-row-card {
    background: rgba(18, 26, 43, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 6px 10px;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}
.fleet-row-card.is-active-target {
    border-color: #00E5FF;
    background: rgba(0, 229, 255, 0.08);
}
.fleet-meta-left {
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
    overflow: hidden;
}
.fleet-status-pill {
    padding: 2px 6px;
    border-radius: 10px;
    font-size: 0.72rem;
    font-weight: 700;
}
.pill-moving { background: rgba(0, 255, 102, 0.15); color: #00FF66; }
.pill-stopped { background: rgba(255, 171, 0, 0.15); color: #FFAB00; }
.pill-stale { background: rgba(148, 163, 184, 0.15); color: #94A3B8; }

/* Small buttons styling */
div[data-testid="stButton"] > button {
    border-radius: 6px !important;
    padding: 3px 8px !important;
    font-size: 0.8rem !important;
    font-weight: 600 !important;
    min-height: 32px !important;
}
</style>
"""


def resolve_api_key() -> tuple[str | None, str]:
    try:
        key = st.secrets.get("OTD_API_KEY")
        if key:
            return str(key), "secrets"
    except Exception:
        pass
    key = os.environ.get("OTD_API_KEY")
    if key:
        return key, "environment"
    typed = st.sidebar.text_input("🔑 OTD API Key", type="password",
                                  help="Delhi Open Transit Data API key. Safe for this session.")
    return (typed.strip() or None), "sidebar"


def rerun_fragment() -> None:
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


# =============================================================================
# 7. MAIN APPLICATION
# =============================================================================

def main() -> None:
    st.set_page_config(layout="wide", page_title="Delhi Transit Radar", page_icon="🚍")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    if "view_state" not in st.session_state:
        st.session_state.view_state = pdk.ViewState(pitch=0, bearing=0, **DEFAULT_VIEW)
    if "target_bus" not in st.session_state:
        st.session_state.target_bus = "None"

    # ---------------- Sidebar Controls ----------------
    sb = st.sidebar
    sb.markdown("### 🗺️ Radar Settings")
    map_theme_choice = sb.radio("Map Style:", ["Dark Neon (Night)", "Light Street (Day)"], index=0)
    map_style_url = CARTO_DARK if "Dark" in map_theme_choice else CARTO_LIGHT

    selected_route = sb.selectbox("Filter Route:", ["All Routes"] + ROUTE_NAMES)

    lock_camera = sb.toggle("🔒 Camera Lock on Bus", value=True,
                            help="Automatically keeps the tracked vehicle centered in view.")
    follow_zoom = sb.slider("Tracking Zoom", 12.0, 17.0, 14.8, 0.2)
    show_trail = sb.toggle("🧵 Show Movement Trail", value=True)

    sb.divider()
    sb.markdown("### ⏱️ Refresh Rate")
    auto_refresh = sb.toggle("Live Auto-Refresh", value=True)
    refresh_rate = int(sb.number_input("Interval (seconds):", min_value=2, max_value=60, value=4))
    if sb.button("🔄 Refresh Now", use_container_width=True):
        st.rerun()

    sb.divider()
    sb.markdown("### 📍 My Stop (Optional)")
    use_my_stop = sb.toggle("Calculate Distance to My Stop", value=False)
    my_lat = sb.number_input("Latitude", value=28.6190, format="%.5f", disabled=not use_my_stop)
    my_lon = sb.number_input("Longitude", value=77.0321, format="%.5f", disabled=not use_my_stop)

    sb.divider()
    api_key, _ = resolve_api_key()
    if not api_key:
        st.error("🔑 OTD API Key required. Enter your Delhi Open Transit Data API key in the sidebar or `.streamlit/secrets.toml`.")
        st.stop()

    tracker = get_tracker()

    # ---------------- Live Fragment (Self-Updating) ----------------
    def live_view() -> None:
        # 1. Ingest Data
        error_msg = None
        snapshot = None
        try:
            snapshot = fetch_feed(api_key)
        except FeedError as exc:
            error_msg = str(exc)
        except Exception as exc:
            error_msg = f"Network issue: {type(exc).__name__}"

        if snapshot is not None:
            tracker.update(snapshot)
        else:
            tracker.record_error(error_msg or "Unknown error")

        now = time.time()
        health = tracker.get_health()
        all_views = tracker.get_views(now)

        # Filter by chosen route
        views = [v for v in all_views if selected_route in ("All Routes", v["route"])]
        views.sort(key=lambda v: (v["route"], v["direction"], v["vehicle_id"]))

        # Distance calculation
        if use_my_stop:
            for v in views:
                v["dist_km"] = round(haversine_m(v["lat"], v["lon"], my_lat, my_lon) / 1000, 2)

        # 2. Header Bar
        last_time_str = fmt_ist(health["last_ok_wall"]) if health["last_ok_wall"] else "Connecting..."
        feed_info = f"{health['total_feed_vehicles']} buses in Delhi" if health['total_feed_vehicles'] else "Live Radar"
        header_html = (
            f'<div class="app-header-strip">'
            f'<div class="app-brand-title">🚍 Delhi Transit Radar</div>'
            f'<div class="app-feed-meta">Updated {last_time_str} IST · {feed_info}</div>'
            f'</div>'
        )
        st.markdown(header_html, unsafe_allow_html=True)

        if health["last_error"]:
            st.warning(f"⚠️ Feed connection alert: {health['last_error']}. Showing last available data.")

        # 3. Compact 3-Pill Stats Bar (Active, Moving, Stopped - No GPS & Offline removed as requested)
        n_move = sum(v["state"] == "moving" for v in views)
        n_stop = sum(v["state"] == "stopped" for v in views)
        stats_html = (
            f'<div class="stats-strip">'
            f'<div class="stat-pill stat-active"><span class="stat-dot dot-active"></span><span class="stat-val">{len(views)}</span><span class="stat-lbl">Active</span></div>'
            f'<div class="stat-pill stat-moving"><span class="stat-dot dot-moving"></span><span class="stat-val">{n_move}</span><span class="stat-lbl">Moving</span></div>'
            f'<div class="stat-pill stat-stopped"><span class="stat-dot dot-stopped"></span><span class="stat-val">{n_stop}</span><span class="stat-lbl">Stopped</span></div>'
            f'</div>'
        )
        st.markdown(stats_html, unsafe_allow_html=True)

        target_id = st.session_state.target_bus
        target_info = next((v for v in views if v["vehicle_id"] == target_id), None) if target_id != "None" else None

        # 4. Telemetry HUD for Selected Bus (Compact & clean, Compass and Last Fix removed as requested)
        if target_info:
            dist_txt = f'<span style="color:#00E5FF;">📍 {target_info["dist_km"]} km to stop</span>' if (use_my_stop and "dist_km" in target_info) else ""
            hud_col_info, hud_col_btn = st.columns([4, 1.2], vertical_alignment="center")
            with hud_col_info:
                hud_html = (
                    f'<div class="hud-strip">'
                    f'<div class="hud-top-line">'
                    f'<span class="route-badge" style="background:{target_info["neon_bg"]};color:{target_info["neon"]};border:1px solid {target_info["neon"]};">{target_info["route"]}</span>'
                    f'<span class="plate-num">{target_info["vehicle_id"]}</span>'
                    f'<span class="dir-sub">➔ {target_info["direction"]}</span>'
                    f'</div>'
                    f'<div class="hud-metrics-row">'
                    f'<span class="speed-tag">⚡ {target_info["speed_kmh"]} km/h</span>'
                    f'<span>{target_info["status_short"]}</span>'
                    f'{dist_txt}'
                    f'</div>'
                    f'</div>'
                )
                st.markdown(hud_html, unsafe_allow_html=True)
            with hud_col_btn:
                if st.button("✕ Free Roam", key="release_hud_btn", use_container_width=True):
                    st.session_state.target_bus = "None"
                    rerun_fragment()

        # 5. Camera View State Calculation
        if target_info and lock_camera:
            st.session_state.view_state = pdk.ViewState(
                latitude=target_info["lat"],
                longitude=target_info["lon"],
                zoom=follow_zoom,
                pitch=0,
                bearing=0,
            )
        elif not target_info and views:
            st.session_state.view_state = fit_view(
                [(v["lat"], v["lon"]) for v in views],
                st.session_state.view_state,
            )

        # 6. PyDeck Radar Map Layers (Rotatable Bus Icons + Smooth Vertex Transitions)
        layers = []

        # Smooth WebGL Transitions (Slides position smoothly across updates without teleporting)
        layer_transitions = {
            "getPosition": {"duration": 2200, "type": "interpolation"},
            "getAngle": {"duration": 800, "type": "interpolation"},
        }

        # Accurate Movement Trail (Only connects actual street travel, resets on teleport)
        if show_trail and target_info:
            trail_coords = tracker.get_trail(target_info["vehicle_id"])
            if len(trail_coords) >= 2:
                layers.append(pdk.Layer(
                    "PathLayer",
                    data=[{"path": trail_coords}],
                    get_path="path",
                    get_color=[255, 235, 59, 230],
                    get_width=5,
                    width_min_pixels=3,
                    width_max_pixels=6,
                ))

        # Bus Icons with Directional Rotation (Turns to face left, right, up, down as bus travels)
        if views:
            df_views = pd.DataFrame(views)

            # Layer A: Directional Rotatable Bus Icons
            layers.append(pdk.Layer(
                "IconLayer",
                data=df_views,
                get_icon="icon_spec",
                get_position="[lon, lat]",
                get_size=36,
                get_angle="bearing_angle",
                size_scale=1,
                pickable=True,
                transitions=layer_transitions,
            ))

            # Layer B: High-Visibility Route Badge Floating Right Above the Bus
            layers.append(pdk.Layer(
                "TextLayer",
                data=df_views,
                get_position="[lon, lat]",
                get_text="route_short",
                get_color=[255, 255, 255, 255],
                get_size=11,
                get_alignment_baseline="'center'",
                get_text_anchor="'middle'",
                get_pixel_offset=[0, -26],
                background=True,
                get_background_color=[11, 15, 25, 220],
                transitions=layer_transitions,
            ))

        # Target Radar Ring (Centered on tracked bus)
        if target_info:
            df_target = pd.DataFrame([{"lat": target_info["lat"], "lon": target_info["lon"]}])
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=df_target,
                get_position="[lon, lat]",
                get_fill_color=[255, 235, 59, 35],
                get_line_color=[255, 235, 59, 255],
                line_width_min_pixels=3,
                get_radius=30,
                radius_min_pixels=22,
                radius_max_pixels=36,
                stroked=True,
                filled=True,
                transitions=layer_transitions,
            ))

        # My Stop User Marker
        if use_my_stop:
            df_stop = pd.DataFrame([{"lat": my_lat, "lon": my_lon, "label": "📍 My Stop"}])
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=df_stop,
                get_position="[lon, lat]",
                get_fill_color=[0, 229, 255, 220],
                get_line_color=[255, 255, 255, 255],
                line_width_min_pixels=2,
                get_radius=16,
                radius_min_pixels=10,
                radius_max_pixels=18,
                stroked=True,
                filled=True,
            ))

        # Click & Hold Accurate Tooltip
        map_tooltip = {
            "html": (
                '<div style="font-family: -apple-system, sans-serif; font-size: 13px; line-height: 1.4; color: #fff; padding: 2px;">'
                '<div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px;">'
                '<span style="background: {neon_bg}; color: {neon}; border: 1px solid {neon}; padding: 1px 6px; border-radius: 4px; font-weight: 800; font-size: 12px;">{route}</span>'
                '<span style="font-family: monospace; font-weight: 700; font-size: 13px; color: #fff;">{vehicle_id}</span>'
                '</div>'
                '<div style="color: #cbd5e1; font-size: 12px; margin-bottom: 3px;">🏁 <b>{direction}</b></div>'
                '<div style="font-weight: 700; color: #00E5FF; font-size: 12px;">⚡ {speed_kmh} km/h · {status_short}</div>'
                '</div>'
            ),
            "style": {
                "backgroundColor": "rgba(11, 15, 25, 0.95)",
                "backdropFilter": "blur(8px)",
                "border": "1px solid rgba(0, 229, 255, 0.35)",
                "borderRadius": "8px",
                "padding": "8px 12px",
                "boxShadow": "0 8px 24px rgba(0, 0, 0, 0.6)",
            },
        }

        # Render PyDeck Map with CARTO Dark Tiles
        st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=st.session_state.view_state,
                map_style=map_style_url,
                tooltip=map_tooltip,
            ),
            use_container_width=True,
        )

        # 7. High-Density Active Fleet List (Compact, no div errors, direct 1-tap track)
        st.markdown(f"### 🚍 Active Fleet ({len(views)} Live Buses)")
        if views:
            for b in views:
                is_this_tracked = (b["vehicle_id"] == target_id)
                card_active_class = "is-active-target" if is_this_tracked else ""
                pill_class = f"pill-{b['state']}"

                dist_text = f"<span style='color:#00E5FF; font-weight:700; font-size:0.75rem;'>📍 {b['dist_km']} km</span>" if (use_my_stop and "dist_km" in b) else ""

                col_row_info, col_row_btn = st.columns([4, 1.2], vertical_alignment="center")
                with col_row_info:
                    row_html = (
                        f'<div class="fleet-row-card {card_active_class}">'
                        f'<div class="fleet-meta-left">'
                        f'<span class="route-badge" style="background:{b["neon_bg"]};color:{b["neon"]};border:1px solid {b["neon"]};">{b["route"]}</span>'
                        f'<span class="plate-num">{b["vehicle_id"]}</span>'
                        f'<span class="fleet-status-pill {pill_class}">{b["status_short"]}</span>'
                        f'<span class="dir-sub">➔ {b["direction"]}</span>'
                        f'{dist_text}'
                        f'</div>'
                        f'</div>'
                    )
                    st.markdown(row_html, unsafe_allow_html=True)

                with col_row_btn:
                    btn_label = "🔒 Locked" if is_this_tracked else "🎯 Track"
                    if st.button(btn_label, key=f"btn_track_{b['vehicle_id']}", use_container_width=True, disabled=is_this_tracked):
                        st.session_state.target_bus = b["vehicle_id"]
                        rerun_fragment()
        else:
            st.info("No active buses detected on selected route right now.")

    # ---------------- Fragment Execution ----------------
    fragment = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
    if fragment is not None:
        fragment(run_every=refresh_rate if auto_refresh else None)(live_view)()
    else:
        live_view()
        if auto_refresh:
            time.sleep(refresh_rate)
            st.rerun()


if __name__ == "__main__":
    main()
