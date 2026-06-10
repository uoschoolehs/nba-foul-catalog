import re
import streamlit as st
from curl_cffi import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="NBA Live Foul Catalog", layout="wide")
st.title("🏀 NBA Live Foul Catalog")

game_id = st.sidebar.text_input("NBA Game ID", value="0042500403")

# ── Headers ──────────────────────────────────────────────────────────────────
# x-nba-stats-* headers are required by stats.nba.com endpoints.
# impersonate='chrome120' makes curl_cffi spoof the TLS fingerprint — this is
# what actually bypasses Akamai, not just the header strings.
STATS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.nba.com",
    "Pragma": "no-cache",
    "Referer": "https://www.nba.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

CDN_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

IMPERSONATE = "chrome120"

# ── Foul Parser ───────────────────────────────────────────────────────────────
def parse_foul_details(desc, fallback_name="Unknown"):
    if not desc:
        return fallback_name, "Personal"

    lower = desc.lower()

    # Foul type — order matters (most specific first)
    if "flagrant.foul.type2" in lower or "flagrant type 2" in lower:
        foul_type = "Flagrant Type 2"
    elif "flagrant.foul.type1" in lower or "flagrant type 1" in lower:
        foul_type = "Flagrant Type 1"
    elif "flagrant" in lower:
        foul_type = "Flagrant"
    elif "l.b.foul" in lower or "loose ball" in lower:
        foul_type = "Loose Ball"
    elif "personal take foul" in lower or "take foul" in lower or ".take" in lower:
        foul_type = "Take Foul"
    elif "clear path" in lower:
        foul_type = "Clear Path"
    elif "away from play" in lower:
        foul_type = "Away From Play"
    elif "s.foul" in lower or "shooting foul" in lower:
        foul_type = "Shooting"
    elif "off.foul" in lower or "offensive foul" in lower:
        foul_type = "Offensive"
    elif "t.foul" in lower or "technical" in lower:
        foul_type = "Technical"
    elif "p.foul" in lower or "personal foul" in lower:
        foul_type = "Personal"
    else:
        foul_type = "Personal"

    # Player name: everything before the first foul keyword
    FOUL_KEYWORDS = [
        "flagrant", "l.b.foul", "loose", "personal", "s.foul", "p.foul",
        "off.foul", "t.foul", "take foul", "clear path", "away from play",
        "shooting", "offensive", "technical", "foul",
    ]
    # Split on the EARLIEST keyword match position
    split_pos = len(desc)
    for kw in FOUL_KEYWORDS:
        idx = lower.find(kw)
        if idx != -1 and idx < split_pos:
            split_pos = idx

    raw_name = desc[:split_pos].strip().rstrip(".")
    player_name = raw_name if raw_name else fallback_name
    return player_name, foul_type


# ── PBP Fetch ────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False, ttl=300)
def fetch_foul_catalog(gid):
    """Fetch play-by-play and return list of foul dicts. Tries stats then CDN."""
    debug = {"pbp_status": "Not Attempted", "pbp_error": None, "source": None}
    catalog = []

    urls_to_try = [
        (
            f"https://stats.nba.com/stats/playbyplayv3?GameID={gid}&StartPeriod=0&EndPeriod=0",
            STATS_HEADERS,
            "stats.nba.com/playbyplayv3",
            lambda d: d.get("game", {}).get("actions", []),
        ),
        (
            f"https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{gid}.json",
            CDN_HEADERS,
            "cdn.nba.com",
            lambda d: d.get("game", {}).get("actions", []),
        ),
    ]

    actions = []
    for url, hdrs, label, extractor in urls_to_try:
        try:
            r = requests.get(url, headers=hdrs, impersonate=IMPERSONATE, timeout=15)
            debug["pbp_status"] = r.status_code
            if r.status_code == 200:
                actions = extractor(r.json())
                debug["source"] = label
                break
            debug["pbp_error"] = f"{label} → HTTP {r.status_code}"
        except Exception as e:
            debug["pbp_error"] = f"{label} → {e}"

    if not actions:
        return catalog, debug

    foul_idx = 1
    for act in actions:
        atype = str(act.get("actionType", "")).lower()
        sub   = str(act.get("subType", "")).lower()
        desc  = act.get("description", "")

        is_foul = (
            atype == "foul"
            or "foul" in sub
            or "foul" in desc.lower()
            # offensive fouls sometimes logged as turnovers
            or (atype == "turnover" and "offensive" in sub)
        )
        if not is_foul:
            continue

        # actionNumber is the real GameEventID for videoeventsasset
        event_id = act.get("actionNumber")
        if event_id is None:
            continue
        event_id = str(event_id)

        raw_clock = act.get("clock", "PT00M00.00S")
        clean_clock = raw_clock.replace("PT", "").replace("M", ":").replace("S", "")
        clean_clock = clean_clock.split(".")[0]  # drop sub-seconds

        fallback_name = act.get("playerNameI") or act.get("playerName") or "Unknown"
        p_name, f_type = parse_foul_details(desc, fallback_name)

        catalog.append({
            "foul_number": foul_idx,
            "event_id": event_id,
            "quarter": act.get("period", 1),
            "clock": clean_clock,
            "team": act.get("teamTricode", "?"),
            "player": p_name,
            "type": f_type,
            "description": desc,
        })
        foul_idx += 1

    return catalog, debug


# ── Video URL Fetch (single) ──────────────────────────────────────────────────
def _fetch_one_video(gid, event_id):
    """Returns (event_id, mp4_url_or_None)."""
    url = f"https://stats.nba.com/stats/videoeventsasset?GameEventID={event_id}&GameID={gid}"
    try:
        r = requests.get(url, headers=STATS_HEADERS, impersonate=IMPERSONATE, timeout=10)
        if r.status_code != 200:
            return event_id, None
        data = r.json()
        rsets = data.get("resultSets", {})

        video_urls = []
        if isinstance(rsets, dict):
            video_urls = rsets.get("Meta", {}).get("videoUrls", [])
        elif isinstance(rsets, list):
            for rs in rsets:
                if "videoUrls" in rs:
                    video_urls = rs["videoUrls"]
                    break

        for v in video_urls:
            for key in ("lurl", "murl", "hdurl", "sdurl", "vurl"):
                val = v.get(key, "")
                if val and (".mp4" in val.lower() or ".m3u8" in val.lower()):
                    return event_id, val
    except Exception:
        pass
    return event_id, None


# ── Bulk Concurrent Video Fetch ───────────────────────────────────────────────
@st.cache_data(show_spinner=False, ttl=300)
def fetch_all_videos(gid, event_ids_tuple):
    """
    Fires all video asset requests in parallel (max 12 workers).
    Returns dict: {event_id: url_or_None}
    """
    results = {}
    event_ids = list(event_ids_tuple)  # cache requires hashable → tuple in, list out
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_one_video, gid, eid): eid for eid in event_ids}
        for fut in as_completed(futures):
            eid, url = fut.result()
            results[eid] = url
    return results


def fallback_link(gid, entry):
    from urllib.parse import quote
    enc = quote(entry["description"])
    return (
        f"https://www.nba.com/stats/events?"
        f"CFID=&CFPARAMS=&GameEventID={entry['event_id']}"
        f"&GameID={gid}&Season=2025-26&flag=1&title={enc}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# RUNTIME
# ═══════════════════════════════════════════════════════════════════════════════

with st.spinner("Loading play-by-play data..."):
    live_catalog, debug_logs = fetch_foul_catalog(game_id)

# Debug sidebar
st.sidebar.markdown("---")
with st.sidebar.expander("🛠️ Network Debug", expanded=False):
    st.write(f"**PBP Status:** `{debug_logs['pbp_status']}`")
    st.write(f"**Source:** `{debug_logs.get('source', 'None')}`")
    if debug_logs["pbp_error"]:
        st.error(debug_logs["pbp_error"])

if not live_catalog:
    st.error("❌ Could not fetch game data. Check the Network Debug panel in the sidebar.")
    st.stop()

# ── Filters ───────────────────────────────────────────────────────────────────
teams   = sorted(set(i["team"]   for i in live_catalog))
players = sorted(set(i["player"] for i in live_catalog))
types   = sorted(set(i["type"]   for i in live_catalog))

st.sidebar.header("Filter")
filter_team   = st.sidebar.selectbox("Team",   ["All Teams"]   + teams)
filter_player = st.sidebar.selectbox("Player", ["All Players"] + players)
filter_type   = st.sidebar.selectbox("Type",   ["All Types"]   + types)

filtered = [
    m for m in live_catalog
    if (filter_team   == "All Teams"   or m["team"]   == filter_team)
    and (filter_player == "All Players" or m["player"] == filter_player)
    and (filter_type   == "All Types"   or m["type"]   == filter_type)
]

st.metric("Fouls Found", len(filtered))

if not filtered:
    st.info("No fouls match the current filters.")
    st.stop()

# ── Bulk-fetch ALL video URLs up front, concurrently ─────────────────────────
event_ids_tuple = tuple(e["event_id"] for e in filtered)
with st.spinner(f"Fetching {len(filtered)} video links in parallel..."):
    video_map = fetch_all_videos(game_id, event_ids_tuple)

hit  = sum(1 for v in video_map.values() if v)
miss = len(video_map) - hit
st.caption(f"✅ {hit} videos resolved · ⚠️ {miss} unavailable (server-side)")

# ── Playlist Mode ─────────────────────────────────────────────────────────────
st.sidebar.markdown("---")
st.sidebar.header("📺 Playlist Mode")

if "video_index" not in st.session_state:
    st.session_state.video_index = 0
if st.session_state.video_index >= len(filtered):
    st.session_state.video_index = 0

idx = st.session_state.video_index
active = filtered[idx]
active_url = video_map.get(active["event_id"])

st.sidebar.write(f"Clip **{idx + 1}** / **{len(filtered)}**")
st.sidebar.write(f"**{active['player']}** ({active['team']}) — {active['type']}")
st.sidebar.write(f"Q{active['quarter']} · {active['clock']}")

if active_url:
    st.sidebar.video(active_url)
else:
    st.sidebar.warning("Video unavailable from server.")
    st.sidebar.markdown(f"[🔗 View on NBA.com]({fallback_link(game_id, active)})")

col_prev, col_next = st.sidebar.columns(2)
with col_prev:
    if st.button("⬅️ Prev"):
        st.session_state.video_index = (idx - 1) % len(filtered)
        st.rerun()
with col_next:
    if st.button("Next ➡️"):
        st.session_state.video_index = (idx + 1) % len(filtered)
        st.rerun()

# ── Full Index ────────────────────────────────────────────────────────────────
st.markdown("### Clip Index")

for entry in filtered:
    clip_url = video_map.get(entry["event_id"])
    with st.container():
        col1, col2 = st.columns([2, 3])
        with col1:
            st.subheader(f"#{entry['foul_number']} — Q{entry['quarter']} ({entry['clock']})")
            st.write(f"**Player:** {entry['player']} ({entry['team']})")
            st.write(f"**Type:** {entry['type']}")
            st.caption(f"`{entry['description']}`")
        with col2:
            if clip_url:
                st.video(clip_url)
            else:
                st.warning("⚠️ Video unavailable from NBA server.")
                st.markdown(f"[🔗 View on NBA.com]({fallback_link(game_id, entry)})")
        st.markdown("---")
