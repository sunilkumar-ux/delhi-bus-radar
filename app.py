"""
Delhi Transit Radar - Live Bus Fleet Tracking
Real-time tracking of Delhi public transit buses (Delhi OTD GTFS-realtime).
Features:
- Continuous 0-360 degree smooth directional bus rotation
- Strict camera tracking lock (pan disabled, pinch/scroll zoom enabled)
- Preserved map scroll/pan position on feed updates
- Mobile-optimized tap & touch targets with instant tooltips
- High-contrast neon radar styling & zero keyboard intrusion
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
JITTER_M = 3.5           # Distance threshold used by the speed / stopped-timer logic
BEARING_MIN_M = 11.0     # Min displacement before a freshly computed heading is trusted
BEARING_SNAP_M = 30.0    # Above this displacement the new heading is taken outright
TRAIL_MIN_M = 15.0       # Minimum distance to append point to trail
TRAIL_MAX_GAP_M = 350.0  # Reset trail if jump exceeds 350m to avoid chords cutting across city
MAX_SANE_KMH = 105.0
MAX_DT_FOR_SPEED_S = 180
SPEED_HOLD_S = 60
STALE_S = 180
STALE_NO_TS_S = 900
TRAIL_POINTS = 35

# Fast 3s cache for immediate synchronization
FETCH_TTL_S = 3
OFFLINE_MISSING_SNAPSHOTS = 3
OFFLINE_GRACE_S = 45
OFFLINE_KEEP_S = 4 * 3600

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

# Streamlit renamed `use_container_width=True` to `width="stretch"` and prints
# "will be removed after 2025-12-31" on every call. Streamlit Community Cloud
# upgrades itself, so pick whichever spelling the installed runtime supports
# instead of leaving a time-bomb in a live app.
try:
    _ST_VER = tuple(int(p) for p in st.__version__.split(".")[:2])
except Exception:
    _ST_VER = (1, 40)
WIDE = {"width": "stretch"} if _ST_VER >= (1, 49) else {"use_container_width": True}


# =============================================================================
# 2. DIRECTIONAL SVG BUS ICON GENERATOR
# =============================================================================

def generate_bus_svg_icon(color_hex: str) -> dict:
    """
    Generates a top-down transit bus SVG icon pointing North (0 degrees).
    Deck.gl applies get_angle="icon_angle" - the compass bearing converted to
    deck.gl's counter-clockwise convention by to_icon_angle() - to continuously
    rotate the bus to every single degree (0.0 to 360.0).
    """
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60" viewBox="0 0 60 60">
  <rect x="14" y="6" width="32" height="48" rx="8" fill="{color_hex}" fill-opacity="0.28"/>
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

# Pre-generate vector icons for each route's neon color
BUS_ICONS = {
    "#00FF66": generate_bus_svg_icon("#00FF66"),  # Route 718 (Neon Green)
    "#00E5FF": generate_bus_svg_icon("#00E5FF"),  # Route D-068 (Neon Blue)
    "#FF2A6D": generate_bus_svg_icon("#FF2A6D"),  # Route D-9919 (Neon Red)
}


# =============================================================================
# 3. HELPER FUNCTIONS & CONTINUOUS BEARING CALCULATIONS
# =============================================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def calc_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Computes true continuous 0.0 - 360.0 degree forward azimuth heading from point 1 to point 2.
    Ensures every degree of road curvature is accurately tracked.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    initial_bearing = math.degrees(math.atan2(y, x))
    return (initial_bearing + 360.0) % 360.0


def angle_delta(from_deg: float, to_deg: float) -> float:
    """Shortest signed rotation from one compass angle to another, in (-180, 180]."""
    return (to_deg - from_deg + 540.0) % 360.0 - 180.0


def blend_bearing(old_deg: float, new_deg: float, weight: float) -> float:
    """
    Circular blend that always travels the short way round, so a heading moving
    through due north (359 -> 2) never swings 357 degrees backwards.
    """
    return (old_deg + angle_delta(old_deg, new_deg) * weight) % 360.0


def is_quantised_bearing(deg: float | None) -> bool:
    """
    True when the feed's own bearing looks like a cheap compass rather than a real
    heading. Several Delhi GPS vendors emit only 0/90/180/270 (or 45-degree steps),
    which is exactly what makes icons appear to snap to a handful of directions.
    Such values are only ever used as a weak seed, never as a live heading.
    """
    if deg is None:
        return True
    return abs(deg - round(deg / 45.0) * 45.0) < 0.01


def to_icon_angle(compass_deg: float) -> float:
    """
    Convert a compass bearing (clockwise from north) into deck.gl's IconLayer
    `getAngle`, which rotates COUNTER-clockwise.

    deck.gl icon-layer-vertex.glsl rotates by mat2(cos, -sin, sin, cos) and then
    flips Y into clip space, so getAngle=+90 points the icon WEST. Feeding a raw
    compass bearing therefore mirrors every bus across the north-south axis:
    east renders as west, north-east renders as north-west, and only due north
    and due south happen to look correct.
    """
    return (360.0 - (compass_deg % 360.0)) % 360.0


_COMPASS_16 = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
               "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_point(deg: float) -> str:
    """Human-readable 16-point compass label for a bearing, e.g. 112.4 -> 'ESE'."""
    return _COMPASS_16[int((deg % 360.0) / 22.5 + 0.5) % 16]


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

        # STABLE IDENTITY IS MANDATORY.
        # The old fallback was f"route{raw_route}-{len(found)}", i.e. the bus's
        # position in *this* parse. Iteration order is not guaranteed between
        # polls, so such a bus was handed a different identity every 4 seconds:
        # its trail reset, its heading reset, and anything tracking it lost it
        # instantly. A bus with no stable id is better dropped than faked - and
        # in practice the live OTD feed always supplies vehicle.id, so this
        # fallback chain simply never has to invent anything.
        vid = ""
        if _has(veh, "vehicle"):
            vid = (veh.vehicle.id or veh.vehicle.label or "").strip()
        if not vid:
            vid = (entity.id or "").strip()
        if not vid and _has(veh, "trip"):
            vid = (veh.trip.trip_id or "").strip()
        if not vid:
            continue

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
# 5. FLEET TRACKER & CONTINUOUS HEADING ENGINE
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

        # Initial heading estimate based on route destination.
        # The feed's own bearing is only trusted as a seed when it is NOT snapped
        # to a 45-degree step: several Delhi GPS vendors emit nothing but
        # 0/90/180/270, and seeding from those is a direct cause of buses sitting
        # on cardinal directions until they next move.
        initial_bearing = 190.0 if "Kapashera" in info["dir"] else 20.0
        feed_bearing = rec["bearing"]
        seed_measured = False
        if feed_bearing is not None and 0.0 <= feed_bearing < 360.0 \
                and not is_quantised_bearing(feed_bearing):
            initial_bearing = feed_bearing
            seed_measured = True

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
                "bearing": round(initial_bearing, 1),
                # Unwrapped twin of `bearing` expressed in deck.gl's counter-clockwise
                # convention. It is allowed to run past 360 / below 0 so that WebGL's
                # linear getAngle interpolation always takes the short way round
                # instead of spinning 350 degrees backwards when crossing north.
                "icon_angle": to_icon_angle(initial_bearing),
                "bearing_measured": seed_measured,
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

        # Tolerate tracker entries created before these fields existed.
        st_.setdefault("icon_angle", to_icon_angle(st_["bearing"]))
        st_.setdefault("bearing_measured", False)

        # ---- Exact continuous 0.0-360.0 degree heading along the road --------
        # The heading is the true forward azimuth from the previous GPS fix to the
        # latest GPS fix, to one decimal place. Below BEARING_MIN_M the apparent
        # movement is mostly GPS noise, so the last good heading is held rather
        # than letting jitter spin the bus to a random direction.
        if moved_m >= BEARING_MIN_M:
            exact_heading = calc_bearing(st_["lat"], st_["lon"], rec["lat"], rec["lon"])
            if moved_m >= BEARING_SNAP_M:
                new_bearing = exact_heading          # long confident leg: take it as-is
            else:
                # Short leg: ease into it so curves read smoothly. blend_bearing
                # goes the short way round, so 359 -> 2 never swings backwards.
                new_bearing = blend_bearing(st_["bearing"], exact_heading, 0.72)
            st_["bearing"] = round(new_bearing, 1)
            st_["bearing_measured"] = True
            st_["icon_angle"] += angle_delta(st_["icon_angle"] % 360.0,
                                             to_icon_angle(st_["bearing"]))
        elif (not st_["bearing_measured"] and feed_bearing is not None
                and 0.0 <= feed_bearing < 360.0 and not is_quantised_bearing(feed_bearing)):
            # No real trajectory measured yet - a genuine fractional feed bearing
            # still beats the route-direction guess.
            st_["bearing"] = round(feed_bearing, 1)
            st_["icon_angle"] += angle_delta(st_["icon_angle"] % 360.0,
                                             to_icon_angle(st_["bearing"]))

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

        # Trail filtering: only append actual street travel, reset if jump > 350m
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
                    # Bare stop duration, for places too narrow for the full
                    # "Stopped (13m 04s)" wording.
                    "stopped_for": stop_dur if state == "stopped" else "",
                    "bearing": b["bearing"],
                    # True compass heading, clockwise from north, one decimal.
                    "bearing_angle": round(float(b["bearing"]) % 360.0, 1),
                    "heading_text": compass_point(b["bearing"]),
                    # False => the heading is still the route-direction seed and
                    # has never been confirmed by actual movement. The live Delhi
                    # OTD feed ships NO position.bearing at all, so every bus
                    # starts out unmeasured and only earns a real heading once it
                    # has covered BEARING_MIN_M between two fixes.
                    "bearing_measured": bool(b.get("bearing_measured", False)),
                    # deck.gl IconLayer angle: counter-clockwise, and deliberately
                    # left unwrapped so getAngle interpolation takes the short path.
                    "icon_angle": round(float(b.get("icon_angle", to_icon_angle(b["bearing"]))), 1),
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
# 6. UI & MOBILE STYLING (NO VIRTUAL KEYBOARD POPUP)
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

/* ---- Selectbox: no caret, no text selection, FULL tap target -------------
   The original rule put pointer-events:none on the combobox input to stop the
   Android keyboard. Measured in a real browser, that did two things, and only
   one of them was intended:
     - it did NOT stop the keyboard (the listbox still moves focus to the input
       programmatically when it opens), and
     - it killed the tap target: only the ~16px chevron at the far right still
       opened the list, so tapping the middle of the control did nothing at all.
   Keeping the caret/selection suppression and stretching the chevron button
   across the whole control restores a full-width, thumb-sized hit area.      */
div[data-testid="stSelectbox"] input {
    pointer-events: none !important;
    caret-color: transparent !important;
    user-select: none !important;
    -webkit-user-select: none !important;
}
div[data-testid="stSelectbox"] div[role="group"] { position: relative; }
div[data-testid="stSelectbox"] div[role="group"] > button {
    position: absolute !important;
    top: 0; left: 0; right: 0; bottom: 0;
    width: 100% !important;
    height: 100% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: flex-end !important;
    background: transparent !important;
    border: none !important;
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

/* Compact 3-Pill Stats Bar */
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

/* Tracking HUD Card */
.hud-strip {
    background: linear-gradient(135deg, rgba(15, 23, 42, 0.95), rgba(24, 33, 53, 0.9));
    border: 1px solid rgba(0, 229, 255, 0.35);
    border-radius: 10px;
    padding: 8px 12px;
    margin: 4px 0 8px 0;
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
    /* nowrap: a long destination must truncate, not spill onto a second line
       and double the height of every single row. */
    flex-wrap: nowrap;
    overflow: hidden;
    min-width: 0;
}
.fleet-meta-left > * { flex: 0 0 auto; }
.fleet-meta-left .dir-sub {
    flex: 0 1 auto;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* ---- Compact fleet list --------------------------------------------------
   Streamlit stacks st.columns vertically on narrow screens, which is exactly
   what made each bus occupy two tall rows on a phone. Forcing nowrap on these
   specific horizontal blocks keeps the Track button inline. Colours are
   deliberately untouched.                                                   */
.st-key-fleetlist div[data-testid="stHorizontalBlock"],
.st-key-hudrow div[data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    gap: 6px !important;
    align-items: center !important;
}
.st-key-fleetlist div[data-testid="stColumn"],
.st-key-hudrow div[data-testid="stColumn"] {
    min-width: 0 !important;
    flex-shrink: 1 !important;
}
.st-key-fleetlist div[data-testid="stColumn"]:last-child,
.st-key-hudrow div[data-testid="stColumn"]:last-child {
    flex: 0 0 auto !important;
    width: auto !important;
}
.st-key-fleetlist [data-testid="stVerticalBlock"] { gap: 0 !important; }
.st-key-fleetlist .fleet-row-card { margin-bottom: 4px; padding: 4px 8px; }
.st-key-fleetlist div[data-testid="stButton"] > button {
    padding: 2px 8px !important;
    min-height: 28px !important;
    height: 28px !important;
    font-size: 0.85rem !important;
}
/* Pan-lock chip + stop button in the tracking HUD row */
.st-key-pan_lock_btn button {
    background: rgba(0, 229, 255, 0.10) !important;
    border: 1px solid rgba(0, 229, 255, 0.45) !important;
    color: #7fe9ff !important;
    font-size: 0.78rem !important;
    font-weight: 700 !important;
    white-space: nowrap !important;
    padding: 2px 10px !important;
    min-height: 30px !important;
    width: 100% !important;
}
.st-key-pan_lock_btn button:hover,
.st-key-pan_lock_btn button:focus,
.st-key-pan_lock_btn button:active {
    background: rgba(0, 229, 255, 0.20) !important;
    border-color: #00E5FF !important;
    color: #ffffff !important;
}
.st-key-stop_track_btn button {
    background: rgba(148, 163, 184, 0.12) !important;
    border: 1px solid rgba(148, 163, 184, 0.3) !important;
    color: #cbd5e1 !important;
    min-height: 30px !important;
    padding: 2px 10px !important;
}
.hud-lost {
    color: #FFAB00;
    font-weight: 700;
}

/* ---- Narrow phones: claw back vertical space --------------------------
   Same colours, just a tighter header so the map starts higher up.       */
@media (max-width: 480px) {
    .app-header-strip { flex-wrap: nowrap; gap: 8px; }
    .app-brand-title { font-size: 1.02rem; white-space: nowrap; }
    .app-feed-meta { font-size: 0.64rem; text-align: right; }
    .block-container { padding-top: 2.2rem !important; }
    .hud-strip { padding: 6px 10px; }
    .hud-metrics-row { font-size: 0.78rem; gap: 9px; }
    .hud-top-line { gap: 6px; margin-bottom: 4px; }
    .hud-top-line .dir-sub { font-size: 0.72rem; }
    .st-key-pan_lock_btn button { font-size: 0.72rem !important; padding: 2px 6px !important; }
    .tap-head { padding-right: 118px; }
    /* "Active Fleet (16 Live Buses)" wrapped onto two tall lines */
    h3 { font-size: 1.02rem !important; white-space: nowrap; margin-bottom: 4px !important; }
    .st-key-fleetlist .plate-num { font-size: 0.86rem; }
    .st-key-fleetlist .route-badge { font-size: 0.74rem; padding: 2px 6px; }
    .st-key-fleetlist .fleet-status-pill { font-size: 0.66rem; }
    .st-key-fleetlist .dir-sub { font-size: 0.72rem; }
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

/* Deck.gl Tooltip mobile overrides */
.deck-tooltip {
    pointer-events: none !important;
    z-index: 999999 !important;
    max-width: 280px !important;
    white-space: normal !important;
}

/* Tap-to-inspect detail card (the touch-friendly replacement for hover) */
.tap-card {
    background: linear-gradient(135deg, rgba(15, 23, 42, 0.97), rgba(24, 33, 53, 0.93));
    border: 1px solid rgba(0, 229, 255, 0.35);
    border-radius: 12px;
    padding: 12px 14px;
    margin: 10px 0 6px 0;
    box-shadow: 0 6px 22px rgba(0, 0, 0, 0.5);
}
.tap-head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
    /* Reserve the top-right corner for the floating Track / close controls. */
    padding-right: 124px;
    min-height: 26px;
}
.tap-est {
    font-size: 0.68rem;
    font-weight: 700;
    color: #94a3b8;
    border: 1px solid rgba(148, 163, 184, 0.35);
    border-radius: 4px;
    padding: 0 4px;
    margin-left: 4px;
}
.tap-trend {
    font-size: 0.72rem;
    font-weight: 700;
    color: #94a3b8;
    margin-left: 4px;
}

/* ---- Floating controls inside the tap-detail card -----------------------
   Streamlit >=1.39 stamps a `st-key-<key>` class onto every keyed element, so
   the card container and its two buttons can be targeted precisely without
   relying on DOM order. Both buttons are taken out of flow, which is why they
   add no height to the card at all.                                        */
.st-key-tapcard { position: relative; }
.st-key-tapcard .st-key-tap_track_btn {
    position: absolute;
    top: 20px;
    right: 56px;
    width: auto !important;
    z-index: 6;
}
.st-key-tapcard .st-key-tap_close_btn {
    position: absolute;
    top: 20px;
    right: 16px;
    width: auto !important;
    z-index: 6;
}
.st-key-tap_track_btn button {
    background: #FFF27A !important;
    border: 1px solid #FFE14D !important;
    color: #0c1222 !important;
    font-weight: 800 !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.2px;
    padding: 2px 9px !important;
    min-height: 26px !important;
    height: 26px !important;
    border-radius: 6px !important;
    box-shadow: 0 0 10px rgba(255, 242, 122, 0.35) !important;
    white-space: nowrap;
}
.st-key-tap_track_btn button:hover,
.st-key-tap_track_btn button:focus,
.st-key-tap_track_btn button:active {
    background: #FFE14D !important;
    border-color: #FFF27A !important;
    color: #0c1222 !important;
    box-shadow: 0 0 14px rgba(255, 242, 122, 0.55) !important;
}
.st-key-tap_close_btn button {
    background: rgba(148, 163, 184, 0.12) !important;
    border: 1px solid rgba(148, 163, 184, 0.3) !important;
    color: #cbd5e1 !important;
    font-size: 0.72rem !important;
    padding: 2px 8px !important;
    min-height: 26px !important;
    height: 26px !important;
    border-radius: 6px !important;
}
.tap-dir {
    font-size: 0.95rem;
    font-weight: 800;
    margin-bottom: 8px;
}
.tap-grid {
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.tap-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 3px 0;
    border-bottom: 1px dashed rgba(148, 163, 184, 0.14);
}
.tap-row:last-child { border-bottom: none; }
.tap-k { color: #94a3b8; font-size: 0.78rem; }
.tap-v {
    color: #e2e8f0;
    font-size: 0.80rem;
    font-weight: 700;
    text-align: right;
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


# ---------------------------------------------------------------------------
# Tracking state.
#
# TARGET_KEY is now PLAIN session state - it is deliberately NOT the key of any
# widget any more. That one change kills an entire class of bug.
#
# WHY: Streamlit serialises a selectbox's value as its *formatted label string*
# (SelectboxSerde.serialize -> format_func(value)) and deserialises it with
#     option_index = formatted_option_to_option_index.get(ui_value)
#     return self.options[option_index] if option_index is not None else ui_value
# The final `else ui_value` is the trap. The old picker's label embedded the bus's
# live speed - "D-068 - DL1PD7776 -> Towards Dwarka Mor (36.8 km/h)" - which
# changes on literally every 4-second poll. So the label the browser echoed back
# no longer existed in the rebuilt options map, the lookup missed, and Streamlit
# wrote the stale LABEL into st.session_state["target_bus"]. From that moment
# `next(v for v in views if v["vehicle_id"] == target_id)` could never match,
# the HUD vanished and the app printed the label verbatim inside
# "Bus <label> is not reporting right now" - permanently, because every later
# poll re-fed the corrupted value through the same path.
# No widget owns this key now, so no serde ever touches it.
TARGET_KEY = "target_bus"
TAPPED_KEY = "tapped_bus"
PANLOCK_KEY = "pan_lock"
# Last good view-dict of the tracked bus, so a dropped GPS fix does not yank the
# camera and the HUD away mid-journey.
TRACK_SNAP_KEY = "_target_snapshot"
TRACK_LOST_AT_KEY = "_target_lost_at"
PANLOCK_PREV_KEY = "_pan_lock_prev"
# Delhi's feed is genuinely patchy - gaps of 1-3 minutes are normal in traffic.
# Hold the lock through that instead of dumping the user back to free roam.
TRACK_AUTO_RELEASE_S = 300.0


def start_track(vid: str) -> None:
    """Begin (or switch) tracking. Never touches TAPPED_KEY."""
    if not vid or vid == "None":
        return
    st.session_state[TARGET_KEY] = vid
    st.session_state[TRACK_SNAP_KEY] = None
    st.session_state[TRACK_LOST_AT_KEY] = None


def stop_track() -> None:
    """Release the camera lock. Never touches TAPPED_KEY."""
    st.session_state[TARGET_KEY] = "None"
    st.session_state[TRACK_SNAP_KEY] = None
    st.session_state[TRACK_LOST_AT_KEY] = None


def toggle_track(vid: str) -> None:
    """
    on_click callback for every Track control.

    Deliberately leaves TAPPED_KEY alone: the tap-detail card must stay exactly
    as it is when its Track button is pressed. Streamlit runs callbacks BEFORE
    the rerun, so the rest of the script sees the new value immediately.
    """
    if st.session_state.get(TARGET_KEY, "None") == vid:
        stop_track()
    else:
        start_track(vid)


def toggle_pan_lock() -> None:
    """Flip the camera between 'glued to the bus' and 'free to pan'."""
    st.session_state[PANLOCK_KEY] = not bool(st.session_state.get(PANLOCK_KEY, True))


def clear_tapped() -> None:
    st.session_state[TAPPED_KEY] = None


# =============================================================================
# 7. MAIN APPLICATION
# =============================================================================

def main() -> None:
    st.set_page_config(layout="wide", page_title="Delhi Transit Radar", page_icon="🚍")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    if "view_state" not in st.session_state:
        st.session_state.view_state = pdk.ViewState(pitch=0, bearing=0, **DEFAULT_VIEW)
    if TARGET_KEY not in st.session_state:
        st.session_state[TARGET_KEY] = "None"
    if TAPPED_KEY not in st.session_state:
        st.session_state[TAPPED_KEY] = None
    if PANLOCK_KEY not in st.session_state:
        st.session_state[PANLOCK_KEY] = True
    if TRACK_SNAP_KEY not in st.session_state:
        st.session_state[TRACK_SNAP_KEY] = None
    if TRACK_LOST_AT_KEY not in st.session_state:
        st.session_state[TRACK_LOST_AT_KEY] = None
    if PANLOCK_PREV_KEY not in st.session_state:
        st.session_state[PANLOCK_PREV_KEY] = True
    if "view_initialized" not in st.session_state:
        st.session_state.view_initialized = False

    # ---------------- Sidebar Controls ----------------
    sb = st.sidebar
    sb.markdown("### 🗺️ Radar Settings")
    map_theme_choice = sb.radio("Map Style:", ["Dark Neon (Night)", "Light Street (Day)"], index=0)
    map_style_url = CARTO_DARK if "Dark" in map_theme_choice else CARTO_LIGHT

    selected_route = sb.selectbox("Filter Route:", ["All Routes"] + ROUTE_NAMES)

    follow_zoom = sb.slider("Follow Zoom (When Locked):", 12.0, 17.5, 15.0, 0.2)
    show_trail = sb.toggle("🧵 Show Movement Trail", value=True)

    sb.divider()
    sb.markdown("### ⏱️ Refresh Rate")
    auto_refresh = sb.toggle("Live Auto-Refresh", value=True)
    refresh_rate = int(sb.number_input("Interval (seconds):", min_value=2, max_value=60, value=4))
    if sb.button("🔄 Refresh Now", **WIDE):
        st.rerun()

    sb.divider()
    sb.markdown("### 📍 My Stop (Optional)")
    use_my_stop = sb.toggle(
        "Calculate Distance to My Stop",
        value=False,
        help="Great-circle (crow-flies) distance between the bus and your stop. "
             "It is NOT the road distance the bus still has to drive, and it is "
             "not an ETA. Road distance would need a routing engine or the full "
             "Delhi GTFS shapes file (~124 MB), neither of which fits here.",
    )
    if use_my_stop:
        sb.caption("📏 Straight-line distance, not road distance. "
                   "Real road distance is typically 1.3–1.6× this.")
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

        # Distance calculation.
        #
        # haversine_m is a GREAT-CIRCLE (straight-line) distance - the direct
        # crow-flies gap between the bus and your stop. It is NOT the distance the
        # bus still has to drive: that would need either a routing engine (OSRM /
        # Google Directions) or the GTFS shapes.txt geometry for these routes, and
        # the full Delhi static GTFS is ~124 MB unzipped, which will not fit in a
        # free Streamlit Cloud container. Road distance in Dwarka typically runs
        # 1.3-1.6x the straight line. The UI now says "straight-line" so the number
        # is never mistaken for an ETA, and pairs it with a cheap but genuinely
        # useful signal: is the bus pointing toward your stop or away from it?
        if use_my_stop:
            for v in all_views:
                v["dist_km"] = round(haversine_m(v["lat"], v["lon"], my_lat, my_lon) / 1000, 2)
                brg_to_stop = calc_bearing(v["lat"], v["lon"], my_lat, my_lon)
                off = abs(angle_delta(v["bearing_angle"], brg_to_stop))
                if not v.get("bearing_measured") or v["state"] != "moving":
                    v["stop_trend"] = ""
                elif off <= 55.0:
                    v["stop_trend"] = "↘ approaching"
                elif off >= 125.0:
                    v["stop_trend"] = "↗ moving away"
                else:
                    v["stop_trend"] = "→ crossing"

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

        # 3. Compact 3-Pill Stats Bar
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

        # 4. Resolve the tracked bus.
        #
        # The dropdown is gone (it ate vertical space, popped the Android
        # keyboard, and its serde silently corrupted the tracking state - see the
        # long note next to TARGET_KEY). Tracking is now driven purely by the
        # Track buttons, with `target_bus` held as ordinary session state.
        target_id = st.session_state.get(TARGET_KEY, "None")

        # Resolve against the UNFILTERED fleet. Looking it up in the
        # route-filtered list meant that merely changing "Filter Route:" made the
        # app believe the tracked bus had gone off the air.
        live_target = (
            next((v for v in all_views if v["vehicle_id"] == target_id), None)
            if target_id != "None" else None
        )

        target_info = None
        target_lost_s = 0.0
        if live_target is not None:
            target_info = live_target
            st.session_state[TRACK_SNAP_KEY] = live_target
            st.session_state[TRACK_LOST_AT_KEY] = None
        elif target_id != "None":
            # The bus missed a few polls. Real Delhi coverage drops out for a
            # minute or two all the time, so hold the lock on the last known fix
            # rather than throwing the user back to free roam. Only give up after
            # TRACK_AUTO_RELEASE_S of total silence.
            snap = st.session_state.get(TRACK_SNAP_KEY)
            if snap and snap.get("vehicle_id") == target_id:
                if st.session_state.get(TRACK_LOST_AT_KEY) is None:
                    st.session_state[TRACK_LOST_AT_KEY] = now
                target_lost_s = max(0.0, now - float(st.session_state[TRACK_LOST_AT_KEY]))
                if target_lost_s >= TRACK_AUTO_RELEASE_S:
                    stop_track()
                    target_id = "None"
                else:
                    target_info = snap
            else:
                stop_track()
                target_id = "None"

        tracking = target_info is not None
        signal_lost = tracking and live_target is None

        # 5. Telemetry HUD + compact pan-lock / stop controls
        if tracking:
            dist_txt = (
                f'<span style="color:#00E5FF;">📍 {target_info["dist_km"]} km to stop</span>'
                if (use_my_stop and "dist_km" in target_info) else ""
            )
            lost_txt = (
                f'<span class="hud-lost">📡 signal lost {format_duration(target_lost_s)}</span>'
                if signal_lost else ""
            )
            # status_short already embeds the speed for a moving bus, so putting it
            # next to the ⚡ speed tag printed "⚡ 16.0 km/h  🟢 16.0 km/h" - the same
            # number twice, eating a third of the strip on a phone. "Stopped
            # (13m 04s)" is likewise too long for a 250px column and pushed the
            # strip onto a third line, so the HUD keeps only the headline figure.
            # The tap card still shows the full, unabbreviated wording.
            _state = target_info["state"]
            if _state == "moving":
                state_txt = "🟢 Moving"
            elif _state == "stale":
                state_txt = "📡 No GPS"
            else:
                _d = (target_info.get("stopped_for") or "").split(" ")[0]
                state_txt = f"🛑 Stopped {_d}" if _d else "🛑 Stopped"
            with st.container(key="hudrow"):
                hud_col_info, hud_col_lock, hud_col_btn = st.columns(
                    [1, 0.26, 0.17], vertical_alignment="center"
                )
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
                        f'<span>{state_txt}</span>'
                        f'{dist_txt}{lost_txt}'
                        f'</div>'
                        f'</div>'
                    )
                    st.markdown(hud_html, unsafe_allow_html=True)
                with hud_col_lock:
                    # A button, not st.toggle: a toggle renders its caption as a
                    # separate label node that truncates to "🔒 Pa..." in a narrow
                    # phone column, and drags a (?) help icon along with it. A
                    # button owns its text, so it can be kept on one line.
                    locked_now = bool(st.session_state.get(PANLOCK_KEY, True))
                    st.button(
                        "🔒 Pan" if locked_now else "🔓 Pan",
                        key="pan_lock_btn",
                        help=("Map panning is LOCKED to the bus — tap to pan freely "
                              "(zoom always works)")
                        if locked_now else
                        ("Map panning is FREE — tap to re-lock the camera onto the bus"),
                        on_click=toggle_pan_lock,
                    )
                with hud_col_btn:
                    st.button("✕", key="stop_track_btn", help="Stop tracking",
                              on_click=stop_track)

        # 6. Map Viewport & Camera Controller
        # - Tracking + Pan Lock ON : camera glued to the bus; dragging disabled,
        #                            every zoom gesture still allowed.
        # - Tracking + Pan Lock OFF: bus stays tracked (HUD, trail, reticle) but
        #                            the camera is handed back to the user.
        # - Free Roam              : user's own map position is preserved and is
        #                            never reset by a refresh.
        pan_locked = bool(st.session_state.get(PANLOCK_KEY, True))

        # Unlocking must not teleport the map. Seed the free camera with wherever
        # the locked camera currently is, so the handover is seamless.
        if st.session_state.get(PANLOCK_PREV_KEY, True) != pan_locked:
            if not pan_locked and target_info is not None:
                st.session_state.view_state = pdk.ViewState(
                    latitude=target_info["lat"], longitude=target_info["lon"],
                    zoom=follow_zoom, pitch=0, bearing=0,
                )
                st.session_state.view_initialized = True
            st.session_state[PANLOCK_PREV_KEY] = pan_locked

        if target_info is not None and pan_locked:
            view_state = pdk.ViewState(
                latitude=target_info["lat"],
                longitude=target_info["lon"],
                zoom=follow_zoom,
                pitch=0,
                bearing=0,
            )
            map_view = pdk.View(
                type="MapView",
                controller={
                    "dragPan": False,
                    "dragRotate": False,
                    "scrollZoom": True,
                    "touchZoom": True,
                    "doubleClickZoom": True,
                    "keyboard": False,
                },
            )
        else:
            # Free Roam: calculate fit_view ONLY ONCE on initial load to preserve user's map scroll
            if not st.session_state.view_initialized and views:
                st.session_state.view_state = fit_view([(v["lat"], v["lon"]) for v in views], st.session_state.view_state)
                st.session_state.view_initialized = True

            view_state = st.session_state.view_state
            map_view = pdk.View(
                type="MapView",
                controller=True,
            )

        # 7. PyDeck Radar Map Layers (Continuous Rotations, Accurate Trails, Mobile Touch Hit-Targets)
        layers = []

        # Smooth WebGL transitions (slides position & heading across updates).
        # ScatterplotLayer has no getAngle attribute, so handing it a getAngle
        # transition makes deck.gl's attribute manager complain; position-only
        # transitions are used for those layers.
        layer_transitions = {
            "getPosition": {"duration": 2200, "type": "interpolation"},
        }
        icon_transitions = {
            "getPosition": {"duration": 2200, "type": "interpolation"},
            "getAngle": {"duration": 900, "type": "interpolation"},
        }

        # Accurate Movement Trail (Follows actual street trajectory, resets on jumps)
        if show_trail and target_info:
            trail_coords = tracker.get_trail(target_info["vehicle_id"])
            if len(trail_coords) >= 2:
                layers.append(pdk.Layer(
                    "PathLayer",
                    id="motion-trail",
                    data=[{"path": trail_coords}],
                    get_path="path",
                    get_color=[255, 235, 59, 230],
                    get_width=5,
                    width_min_pixels=3,
                    width_max_pixels=6,
                ))

        # The map draws the filtered fleet PLUS the tracked bus. Without this, a
        # route filter could centre the camera on a bus that was not drawn, and
        # tapping the reticle picked nothing at all.
        render_views = views
        if target_info is not None and not any(
            v["vehicle_id"] == target_info["vehicle_id"] for v in views
        ):
            render_views = views + [target_info]

        if render_views:
            df_views = pd.DataFrame(render_views)

            # Layer A: Generous Mobile Touch Hit-Target (Enables effortless click & hold on phone screen)
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                id="touch-hitbox",
                data=df_views,
                get_position="[lon, lat]",
                get_fill_color=[0, 0, 0, 1],
                get_radius=30,
                radius_min_pixels=28,
                radius_max_pixels=40,
                pickable=True,
                # Streamlit turns on auto-highlight for pickable layers when
                # selections are enabled; deck.gl's default highlight is an opaque
                # navy disc that would sit on top of the bus. Replace it with a
                # soft cyan halo so a tap reads as a deliberate selection glow.
                auto_highlight=True,
                highlight_color=[0, 229, 255, 60],
                transitions=layer_transitions,
            ))

            # Layer B: Top-Down Directional Bus Icons (Rotates to every single degree: 0.0 to 360.0)
            layers.append(pdk.Layer(
                "IconLayer",
                id="bus-icons",
                data=df_views,
                get_icon="icon_spec",
                get_position="[lon, lat]",
                get_size=36,
                # deck.gl rotates icons COUNTER-clockwise, so the clockwise compass
                # bearing is pre-converted in icon_angle. Feeding the raw bearing
                # here mirrors every bus across the north-south axis.
                get_angle="icon_angle",
                size_scale=1,
                billboard=True,
                pickable=True,
                auto_highlight=True,
                highlight_color=[255, 255, 255, 70],
                transitions=icon_transitions,
            ))

            # Layer C: Floating Route Badge Above Each Bus
            layers.append(pdk.Layer(
                "TextLayer",
                id="route-labels",
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
                id="target-reticle",
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

        # My Stop Marker
        if use_my_stop:
            df_stop = pd.DataFrame([{"lat": my_lat, "lon": my_lon, "label": "📍 My Stop"}])
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                id="my-stop-pin",
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

        # Hover tooltip (desktop pointers only - deck.gl tooltips are driven by
        # onHover, which touchscreens never fire. The tap-to-inspect panel below
        # the map is what makes this work on a phone.)
        map_tooltip = {
            "html": (
                '<div style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; font-size: 13px; line-height: 1.45; color: #fff; padding: 2px;">'
                '<div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px;">'
                '<span style="background: {neon_bg}; color: {neon}; border: 1px solid {neon}; padding: 1px 6px; border-radius: 4px; font-weight: 800; font-size: 12px;">{route}</span>'
                '<span style="font-family: monospace; font-weight: 700; font-size: 13px; color: #fff;">{vehicle_id}</span>'
                '</div>'
                '<div style="color: #cbd5e1; font-size: 12px; margin-bottom: 3px;">🏁 <b>{direction}</b></div>'
                '<div style="font-weight: 700; color: #00E5FF; font-size: 12px;">⚡ {speed_kmh} km/h · {status_short}</div>'
                '<div style="color: #94a3b8; font-size: 11px; margin-top: 2px;">🧭 Heading {bearing_angle}° {heading_text} · fix {last_fix_ist}</div>'
                '</div>'
            ),
            "style": {
                "backgroundColor": "rgba(11, 15, 25, 0.96)",
                "backdropFilter": "blur(10px)",
                "border": "1px solid rgba(0, 229, 255, 0.4)",
                "borderRadius": "8px",
                "padding": "10px 14px",
                "boxShadow": "0 8px 24px rgba(0, 0, 0, 0.7)",
                "pointerEvents": "none",
                "zIndex": "999999",
            },
        }

        # Render PyDeck Map with CARTO Dark Tiles & Strict/Free Viewport Controller.
        # on_select="rerun" turns the map into a real input widget, so a finger TAP
        # (which never produces a hover event, and therefore never produced a
        # deck.gl tooltip) is reported back to Python and drives the detail panel
        # underneath. Every layer carries an explicit id, which Streamlit requires
        # for selections to stay stable across reruns.
        map_event = st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=view_state,
                views=[map_view],
                map_style=map_style_url,
                tooltip=map_tooltip,
            ),
            **WIDE,
            key="radar_map",
            on_select="rerun",
            selection_mode="single-object",
        )

        # ---- Tap-to-inspect: resolve whichever bus the user touched -----------
        picked_vid = None
        try:
            sel = getattr(map_event, "selection", None)
            objs = {}
            if isinstance(sel, dict):
                objs = sel.get("objects") or {}
            elif sel is not None:
                objs = getattr(sel, "objects", None) or {}
            for layer_id in ("bus-icons", "touch-hitbox"):
                rows = objs.get(layer_id) or []
                if rows:
                    picked_vid = str(rows[0].get("vehicle_id") or "").strip() or None
                    break
        except Exception:
            picked_vid = None

        # React to CHANGES in the map selection, not to its standing value.
        # The chart keeps its selection across the auto-refresh reruns, so acting
        # on the standing value would make the "Close" button useless - the card
        # would immediately reopen on the next refresh. Comparing against the
        # previous run also means tapping empty map space clears the card.
        if picked_vid != st.session_state.get("_map_sel_prev", "\x00"):
            st.session_state["_map_sel_prev"] = picked_vid
            st.session_state[TAPPED_KEY] = picked_vid

        tapped_id = st.session_state.get(TAPPED_KEY)
        # Resolved against the unfiltered fleet for the same reason as the target:
        # changing the route filter must not blank out a card you just opened.
        tapped = next((v for v in all_views if v["vehicle_id"] == tapped_id), None) if tapped_id else None

        if tapped:
            dist_line = ""
            if use_my_stop and "dist_km" in tapped:
                trend = tapped.get("stop_trend", "")
                trend_html = f'<span class="tap-trend">{trend}</span>' if trend else ""
                dist_line = (
                    f'<div class="tap-row">'
                    f'<span class="tap-k">Straight-line to my stop</span>'
                    f'<span class="tap-v">{tapped["dist_km"]} km {trend_html}</span></div>'
                )

            # Say so when the arrow is still a route-direction guess rather than a
            # heading confirmed by movement. The live OTD feed carries no bearing
            # field whatsoever, so this is the normal state for a bus that has not
            # moved since it was first seen.
            if tapped.get("bearing_measured"):
                heading_v = f'{tapped["bearing_angle"]}° {tapped["heading_text"]}'
            else:
                heading_v = f'{tapped["bearing_angle"]}° {tapped["heading_text"]} <span class="tap-est">est.</span>'

            is_tracked = (target_id == tapped["vehicle_id"])
            with st.container(key="tapcard"):
                st.markdown(
                    f'<div class="tap-card" style="border-color:{tapped["neon"]}55;">'
                    f'<div class="tap-head">'
                    f'<span class="route-badge" style="background:{tapped["neon_bg"]};color:{tapped["neon"]};border:1px solid {tapped["neon"]};">{tapped["route"]}</span>'
                    f'<span class="plate-num">{tapped["vehicle_id"]}</span>'
                    f'</div>'
                    f'<div class="tap-dir" style="color:{tapped["neon"]};">➔ {tapped["direction"]}</div>'
                    f'<div class="tap-grid">'
                    f'<div class="tap-row"><span class="tap-k">Route number</span><span class="tap-v">{tapped["route_short"]}</span></div>'
                    f'<div class="tap-row"><span class="tap-k">Bus number</span><span class="tap-v">{tapped["vehicle_id"]}</span></div>'
                    f'<div class="tap-row"><span class="tap-k">Status</span><span class="tap-v">{tapped["status_text"]}</span></div>'
                    f'<div class="tap-row"><span class="tap-k">Speed</span><span class="tap-v">{tapped["speed_kmh"]} km/h</span></div>'
                    f'<div class="tap-row"><span class="tap-k">Heading</span><span class="tap-v">{heading_v}</span></div>'
                    f'<div class="tap-row"><span class="tap-k">Last GPS fix</span><span class="tap-v">{tapped["last_fix_ist"]} IST · {tapped["age_text"]}</span></div>'
                    f'{dist_line}'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
                # Both controls are absolutely positioned into the card's empty
                # top-right corner by CSS, so they cost no extra vertical space.
                # on_click runs BEFORE the rerun and neither callback touches
                # TAPPED_KEY, so pressing Track leaves this card exactly where it
                # is - only an actual tap on empty map space dismisses it.
                st.button(
                    "🎯 Tracking" if is_tracked else "🎯 Track",
                    key="tap_track_btn",
                    help="Stop tracking this bus" if is_tracked else "Lock the camera on this bus",
                    on_click=toggle_track,
                    args=(tapped["vehicle_id"],),
                )
                st.button("✕", key="tap_close_btn", help="Close", on_click=clear_tapped)
        elif tapped_id:
            # Bus was tapped but has since dropped off the feed entirely.
            st.caption(f"ℹ️ Bus {tapped_id} is no longer reporting.")

        # 8. High-Density Active Fleet List (Compact, 1-tap track)
        #
        # Compaction is done three ways, all in CSS so not one colour changes:
        #   1. the row's horizontal block is forced to flex-wrap:nowrap, so the
        #      Track button stays on the SAME line on a phone instead of being
        #      stacked underneath (that stacking is what doubled the height of
        #      every entry and caused the endless scrolling);
        #   2. the metadata line itself is nowrap + ellipsis, so a long
        #      destination truncates instead of wrapping to a second line;
        #   3. once the list is long it goes inside a fixed-height scroll pane,
        #      so the fleet never pushes the rest of the page off the screen.
        st.markdown(f"### 🚍 Active Fleet ({len(views)} Live Buses)")
        if views:
            fleet_box = (
                st.container(height=340, key="fleetlist")
                if len(views) > 5 else st.container(key="fleetlist")
            )
            with fleet_box:
                for b in views:
                    is_this_tracked = (b["vehicle_id"] == target_id)
                    card_active_class = "is-active-target" if is_this_tracked else ""
                    pill_class = f"pill-{b['state']}"

                    dist_text = f"<span style='color:#00E5FF; font-weight:700; font-size:0.75rem;'>📍 {b['dist_km']} km</span>" if (use_my_stop and "dist_km" in b) else ""

                    # "Towards Kapashera Border" truncates to a useless "Towa..."
                    # in the width a phone can spare. Dropping the redundant
                    # "Towards " prefix (the ➔ already says it) buys 8 characters
                    # and turns it into "Kapashera Bo...", which is actually readable.
                    short_dir = b["direction"]
                    if short_dir.startswith("Towards "):
                        short_dir = short_dir[len("Towards "):]

                    col_row_info, col_row_btn = st.columns([1, 0.3], vertical_alignment="center")
                    with col_row_info:
                        row_html = (
                            f'<div class="fleet-row-card {card_active_class}">'
                            f'<div class="fleet-meta-left">'
                            f'<span class="route-badge" style="background:{b["neon_bg"]};color:{b["neon"]};border:1px solid {b["neon"]};">{b["route"]}</span>'
                            f'<span class="plate-num">{b["vehicle_id"]}</span>'
                            f'<span class="fleet-status-pill {pill_class}">{b["status_short"]}</span>'
                            f'<span class="dir-sub">➔ {short_dir}</span>'
                            f'{dist_text}'
                            f'</div>'
                            f'</div>'
                        )
                        st.markdown(row_html, unsafe_allow_html=True)

                    with col_row_btn:
                        st.button(
                            "🔒" if is_this_tracked else "🎯",
                            key=f"btn_track_{b['vehicle_id']}",
                            help="Stop tracking" if is_this_tracked else f"Track {b['vehicle_id']}",
                            on_click=toggle_track,
                            args=(b["vehicle_id"],),
                        )
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
