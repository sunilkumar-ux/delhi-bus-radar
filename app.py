import streamlit as st
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pydeck as pdk
import pandas as pd
import time
import datetime
import math
from google.transit import gtfs_realtime_pb2

# --- 1. PAGE SETUP ---
st.set_page_config(layout="wide", page_title="Delhi Transit Nav", page_icon="🚍")
st.title("🚍 Advanced Transit Navigation")

# --- 2. GOD-TIER PHYSICS ENGINE ---
def get_distance_meters(lat1, lon1, lat2, lon2):
    R = 6371000 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def to_ist(timestamp):
    dt = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    return dt.strftime("%I:%M:%S %p")

def format_duration(seconds):
    mins, secs = divmod(int(seconds), 60)
    if mins > 0: return f"{mins}m {secs}s"
    return f"{secs}s"

# --- 3. TERMINAL COORDINATES ---
ROUTE_TERMINALS = {
    "D-9919": {"T1": (28.6190, 77.0321, "Towards Dwarka Mor"), "T2": (28.5135, 77.0853, "Towards Kapashera Border")},
    "D-068": {"T1": (28.6190, 77.0321, "Towards Dwarka Mor"), "T2": (28.5520, 77.0580, "Towards Sector 21")},
    "718": {"T1": (28.6210, 77.0560, "Towards Uttam Nagar"), "T2": (28.5135, 77.0853, "Towards Kapashera Border")}
}

# --- 4. SMART MEMORY SYSTEM (FIXED MEMORY LEAK) ---
if 'view_state' not in st.session_state: st.session_state.view_state = pdk.ViewState(latitude=28.5800, longitude=77.0500, zoom=12.5, pitch=0)
if 'bus_memory' not in st.session_state: st.session_state.bus_memory = {}
if 'offline_buses' not in st.session_state: st.session_state.offline_buses = {}
if 'target_bus' not in st.session_state: st.session_state.target_bus = "None"
if 'cached_buses' not in st.session_state: st.session_state.cached_buses = []
if 'toast_shown' not in st.session_state: st.session_state.toast_shown = False # Prevents Toast Spam crashes

# CRITICAL FIX: Only open ONE network session and keep it forever.
if 'http_session' not in st.session_state:
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2)
    session.mount('https://', HTTPAdapter(max_retries=retries))
    st.session_state.http_session = session

MY_ROUTES = {
    "3753": {"route": "D-9919", "dir": "Towards Kapashera Border", "color": [220, 20, 20], "badge": "🔴"},
    "3752": {"route": "D-9919", "dir": "Towards Dwarka Mor", "color": [220, 20, 20], "badge": "🔴"},
    "2804": {"route": "D-068",  "dir": "Towards Sector 21", "color": [20, 100, 220], "badge": "🔵"},
    "2801": {"route": "D-068",  "dir": "Towards Dwarka Mor", "color": [20, 100, 220], "badge": "🔵"},
    "2179": {"route": "718",    "dir": "Towards Kapashera Border", "color": [20, 180, 20], "badge": "🟢"},
    "2176": {"route": "718",    "dir": "Towards Uttam Nagar", "color": [20, 180, 20], "badge": "🟢"}
}

# --- 5. SIDEBAR CONTROLS ---
st.sidebar.header("🎯 Map Settings")
map_theme = st.sidebar.radio("Map Appearance:", ["Dark Mode (Night)", "Light Mode (Day)"])
theme_code = "dark" if "Dark" in map_theme else "light"
selected_route = st.sidebar.radio("Filter Route:", ["All Buses", "D-9919", "D-068", "718"])

st.sidebar.divider()
st.sidebar.header("⏱️ Refresh Controls")
auto_refresh = st.sidebar.toggle("Enable Auto-Refresh", value=True)
refresh_rate = st.sidebar.number_input("Refresh Speed (Seconds):", min_value=1, max_value=60, value=5)

# --- 6. ROBUST NETWORK ENGINE ---
API_KEY = "tuM2vKfg6Zdjl53tFJ45WzwdUcBBWPMI"
URL = f"https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb?key={API_KEY}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
current_time = time.time()

active_buses_data = []
active_bus_ids = set()
connection_success = False

try:
    # Use the saved, single network session
    response = st.session_state.http_session.get(URL, headers=HEADERS, timeout=8)
    if response.status_code == 200:
        connection_success = True
        st.session_state.toast_shown = False # Reset toast flag
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)

        for entity in feed.entity:
            if entity.HasField('vehicle') and entity.vehicle.HasField('position'):
                raw_route_id = str(entity.vehicle.trip.route_id) if entity.vehicle.trip.HasField('route_id') else ""
                
                if raw_route_id in MY_ROUTES:
                    info = MY_ROUTES[raw_route_id]
                    public_route = info["route"]
                    if selected_route != "All Buses" and selected_route != public_route: continue

                    v_id = entity.vehicle.vehicle.id if entity.vehicle.HasField('vehicle') else "Unknown"
                    lat, lon = entity.vehicle.position.latitude, entity.vehicle.position.longitude
                    active_bus_ids.add(v_id)

                    # PHYSICS ENGINE
                    if v_id not in st.session_state.bus_memory:
                        st.session_state.bus_memory[v_id] = {
                            "lat": lat, "lon": lon, "anchor_lat": lat, "anchor_lon": lon, 
                            "last_api_update": current_time, "speed": 0.0, "stop_time": current_time, 
                            "route": public_route, "internal": raw_route_id, "direction": info["dir"],
                            "badge": info["badge"], "color": info["color"]
                        }
                    else:
                        mem = st.session_state.bus_memory[v_id]
                        if lat != mem["lat"] or lon != mem["lon"]:
                            time_diff = current_time - mem["last_api_update"]
                            dist_m = get_distance_meters(mem["lat"], mem["lon"], lat, lon)
                            mem["speed"] = round((dist_m / time_diff) * 3.6, 1) if time_diff > 0 else 0.0
                            
                            anchor_dist = get_distance_meters(mem["anchor_lat"], mem["anchor_lon"], lat, lon)
                            if anchor_dist >= 100:
                                if public_route in ROUTE_TERMINALS:
                                    t1_lat, t1_lon, t1_name = ROUTE_TERMINALS[public_route]["T1"]
                                    t2_lat, t2_lon, t2_name = ROUTE_TERMINALS[public_route]["T2"]
                                    
                                    curr_t1 = get_distance_meters(lat, lon, t1_lat, t1_lon)
                                    curr_t2 = get_distance_meters(lat, lon, t2_lat, t2_lon)
                                    anch_t1 = get_distance_meters(mem["anchor_lat"], mem["anchor_lon"], t1_lat, t1_lon)
                                    anch_t2 = get_distance_meters(mem["anchor_lat"], mem["anchor_lon"], t2_lat, t2_lon)
                                    
                                    if curr_t1 < anch_t1 and curr_t2 > anch_t2: mem["direction"] = t1_name
                                    elif curr_t2 < anch_t2 and curr_t1 > anch_t1: mem["direction"] = t2_name
                                mem["anchor_lat"], mem["anchor_lon"] = lat, lon

                            mem["lat"], mem["lon"], mem["last_api_update"], mem["stop_time"] = lat, lon, current_time, None
                        else:
                            if current_time - mem["last_api_update"] > 25:
                                mem["speed"] = 0.0
                                if mem["stop_time"] is None: mem["stop_time"] = current_time

                    mem = st.session_state.bus_memory[v_id]
                    status_str = f"🛑 Stopped ({format_duration(current_time - mem['stop_time'])})" if mem["stop_time"] else "🟢 Moving"
                    if v_id in st.session_state.offline_buses: del st.session_state.offline_buses[v_id]
                    icon_data = {"url": "https://img.icons8.com/color/48/bus.png", "width": 48, "height": 48, "anchorY": 48}

                    active_buses_data.append({
                        "Route": public_route, "Vehicle ID": v_id, "Internal ID": raw_route_id,
                        "Direction": mem["direction"], "Speed (km/h)": mem["speed"], "Status": status_str,
                        "lat": lat, "lon": lon, "color": mem["color"], "badge": mem["badge"], "icon_data": icon_data
                    })
        st.session_state.cached_buses = active_buses_data 
except Exception as e:
    active_buses_data = st.session_state.cached_buses
    # Prevent toast spam crash by only showing the warning once per disconnection
    if not st.session_state.toast_shown:
        st.toast("⚠️ Weak Signal: Retrying connection...", icon="📡")
        st.session_state.toast_shown = True

# --- 7. SMART TABULAR SORTING ---
def sort_buses(bus):
    return (bus["Route"], bus["Direction"], bus["Vehicle ID"])
active_buses_data.sort(key=sort_buses)

# --- OFFLINE BUS TRACKER ---
if connection_success:
    for old_v_id in list(st.session_state.bus_memory.keys()):
        if old_v_id not in active_bus_ids:
            old_mem = st.session_state.bus_memory[old_v_id]
            if old_v_id not in st.session_state.offline_buses:
                st.session_state.offline_buses[old_v_id] = {
                    "Route": old_mem["route"], "Vehicle ID": old_v_id, "Internal ID": old_mem["internal"],
                    "Direction": old_mem["direction"], "Vanished At": current_time
                }
            del st.session_state.bus_memory[old_v_id]

df_active = pd.DataFrame(active_buses_data)

# --- 8. MOBILE UI: TABULAR TARGETING MENU & STRICT LOCK ---
col_target, _ = st.columns([3, 1])

with col_target:
    target_info = None
    if st.session_state.target_bus != "None":
        for b in active_buses_data:
            if b["Vehicle ID"] == st.session_state.target_bus:
                target_info = b
                break

    if target_info:
        expander_title = f"🎯 Tracking: {target_info['Route']} ➔ {target_info['Direction']} ({target_info['Vehicle ID']})"
        is_expanded = False 
    else:
        expander_title = "🎯 Target & Center Camera on Specific Bus"
        is_expanded = True

    with st.expander(expander_title, expanded=is_expanded):
        options = ["None"]
        format_dict = {"None": "🚫 Free Roam (Do not track any bus)"}
        
        for b in active_buses_data:
            vid = b['Vehicle ID']
            options.append(vid)
            format_dict[vid] = f"{b['badge']} **{b['Route']}** &nbsp; | &nbsp; 🏁 {b['Direction']} &nbsp; | &nbsp; `{vid}`"

        st.session_state.target_bus = st.radio("Select Bus to Track:", options, format_func=lambda x: format_dict[x], label_visibility="collapsed")

    lock_camera = st.toggle("🔒 Strict Tracking Lock", value=True, help="Prevents scrolling and forces camera to stay on the bus.")

# --- 9. CAMERA LOCK LOGIC ---
target_lat, target_lon = None, None
if st.session_state.target_bus != "None" and target_info:
    target_lat, target_lon = target_info["lat"], target_info["lon"]
    
    if lock_camera:
        current_zoom = st.session_state.view_state.zoom 
        st.session_state.view_state = pdk.ViewState(latitude=target_lat, longitude=target_lon, zoom=current_zoom, pitch=0)

# --- 10. UI MAP RENDERING (WITH CRASH-PROOF VIEWS) ---
col1, col2 = st.columns([2.5, 1.5])

with col1:
    layers = []
    if not df_active.empty:
        layers.append(pdk.Layer(
            "IconLayer", data=df_active, get_icon="icon_data", get_size=4, size_scale=10,
            get_position='[lon, lat]', pickable=True, transitions={"getPosition": {"duration": 1000}}
        ))
        layers.append(pdk.Layer(
            "TextLayer", data=df_active, get_position='[lon, lat]', get_text='Route', 
            get_size=16, get_color=[0,0,0,255] if theme_code=="light" else [255,255,255,255],
            get_pixel_offset='[0, 25]', transitions={"getPosition": {"duration": 1000}}
        ))

    if target_lat is not None:
        highlight_data = pd.DataFrame([{"lat": target_lat, "lon": target_lon}])
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=highlight_data, get_position='[lon, lat]',
            get_fill_color=[255, 255, 0, 100], get_line_color=[255, 255, 0, 255],
            get_radius=80, stroked=True, line_width_min_pixels=3
        ))

    if lock_camera and target_lat is not None:
        map_view = pdk.View(type="MapView", controller={"dragPan": False, "scrollZoom": True, "touchZoom": True})
    else:
        map_view = pdk.View(type="MapView", controller=True)

    st.pydeck_chart(pdk.Deck(
        layers=layers, 
        initial_view_state=st.session_state.view_state, 
        views=[map_view],
        map_style=theme_code,
        tooltip={"text": "Route: {Route}\nID: {Vehicle ID}\nSpeed: {Speed (km/h)} km/h\nStatus: {Status}"}
    ))

# --- 11. TABLES ---
with col2:
    st.subheader("🟢 Live Active Buses")
    if not df_active.empty:
        st.dataframe(df_active[["Route", "Vehicle ID", "Direction", "Speed (km/h)", "Status"]], hide_index=True)
    else:
        st.warning("No active buses right now.")

    st.markdown("---")
    st.subheader("🔴 Disappeared / Offline Buses")
    if st.session_state.offline_buses:
        offline_list = []
        for v, data in st.session_state.offline_buses.items():
            data["Offline Duration"] = format_duration(current_time - data["Vanished At"])
            data["Time Vanished"] = to_ist(data["Vanished At"])
            offline_list.append(data)
        
        df_offline = pd.DataFrame(offline_list)
        st.dataframe(df_offline[["Route", "Vehicle ID", "Direction", "Time Vanished", "Offline Duration"]], hide_index=True)
    else:
        st.info("No buses have disappeared during this session.")

# --- AUTO REFRESH ---
if auto_refresh:
    time.sleep(refresh_rate)
    st.rerun()
