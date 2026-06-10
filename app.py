import os
import json
import re
import streamlit as st
from curl_cffi import requests

st.set_page_config(page_title="NBA Live Foul Catalog", layout="wide")
st.title("🏀 NBA Live Search & Play Catalog")

# Input field so users can change the game ID dynamically on the live site
game_id = st.sidebar.text_input("NBA Game ID Input", value="0042500403")

# Comprehensive headers to resemble an authentic browser request
CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept-Language": "en-US,en;q=0.9"
}

def parse_foul_details(desc):
    if not desc:
        return "Unknown", "Personal"
    clean_desc = re.split(r'\s*\(', desc)[0].strip()
    clean_desc = re.sub(r'flagrant[- ]type[- ]?[12]', 'flagrant', clean_desc, flags=re.IGNORECASE)
    clean_desc = re.sub(r'\b(p\.?foul|s\.?foul|t\.?foul|off\.?foul)\b', '', clean_desc, flags=re.IGNORECASE)
    
    foul_type = "Personal"
    lower_desc = clean_desc.lower()
    
    if "loose ball" in lower_desc: foul_type = "Loose Ball"
    elif "s.foul" in lower_desc or "shooting" in lower_desc: foul_type = "Shooting"
    elif "p.foul" in lower_desc: foul_type = "Personal"
    elif "off.foul" in lower_desc or "offensive" in lower_desc: foul_type = "Offensive"
    elif "t.foul" in lower_desc or "technical" in lower_desc: foul_type = "Technical"
    elif "take" in lower_desc: foul_type = "Take Foul"
    elif "clear path" in lower_desc: foul_type = "Clear Path"
    elif "flagrant" in lower_desc:
        if "2" in lower_desc or "type 2" in lower_desc: foul_type = "Flagrant Type 2"
        elif "1" in lower_desc or "type 1" in lower_desc: foul_type = "Flagrant Type 1"
        else: foul_type = "Flagrant"
    elif "away from play" in lower_desc: foul_type = "Away From Play"
    
    split_keywords = ["loose", "personal", "s.foul", "p.foul", "off.foul", "t.foul", "shooting", "offensive", "technical", "take", "foul", "flagrant"]
    words = clean_desc.split(" ")
    player_words = []
    for word in words:
        if word.lower() in word.lower() in split_keywords or word.upper() in ["FOUL", "L.B.FOUL"]:
            break
        player_words.append(word)
        
    player_name = " ".join(player_words).strip() if player_words else "Unknown"
    return player_name, foul_type

def fetch_unthrottled_cdn_catalog(gid):
    """
    Fetches the play-by-play metadata and video event parameters directly 
    from the NBA APIs using an unthrottled browser impersonator context.
    """
    # Structuring standard debug metrics dictionary
    debug_metrics = {
        "pbp_status": "Not Attempted",
        "video_status": "Not Attempted",
        "video_keys_found": 0,
        "pbp_error": None,
        "video_error": None,
        "raw_video_sample": None
    }
    catalog = []
    
    # Endpoints for official NBA play-by-play structure and standard asset maps
    pbp_url = f"https://stats.nba.com/stats/playbyplayv3?GameID={gid}&StartPeriod=0&EndPeriod=0"
    video_cdn_url = f"https://content-api-prod.nba.com/public/1/video/game/{gid}"
    
    # 1. Pull Play-By-Play structural feed
    try:
        pbp_res = requests.get(pbp_url, headers=CHROME_HEADERS, timeout=10, impersonate="chrome110")
        debug_metrics["pbp_status"] = pbp_res.status_code
        if pbp_res.status_code != 200:
            debug_metrics["pbp_error"] = f"Non-200 return payload status code: {pbp_res.status_code}"
            return catalog, debug_metrics
        pbp_data = pbp_res.json()
    except Exception as e:
        debug_metrics["pbp_status"] = "Exception Failed"
        debug_metrics["pbp_error"] = str(e)
        return catalog, debug_metrics

    # 2. Pull Accompanying Video Event assets mapping
    video_map = {}
    try:
        vid_res = requests.get(video_cdn_url, headers=CHROME_HEADERS, timeout=10, impersonate="chrome110")
        debug_metrics["video_status"] = vid_res.status_code
        if vid_res.status_code == 200:
            vid_data = vid_res.json()
            # Try to grab a sample for the visual debugger object view
            if "results" in vid_data and "items" in vid_data["results"]:
                items = vid_data["results"]["items"]
                if items:
                    debug_metrics["raw_video_sample"] = items[0]
                # Map out individual structural video assets via action / event IDs
                for item in items:
                    # NBA video endpoints track video events relative to eventId integers
                    ev_id = item.get("eventId")
                    v_url = item.get("videoUrl") or item.get("playbyplayUrl")
                    if ev_id and v_url:
                        video_map[str(ev_id)] = v_url
            debug_metrics["video_keys_found"] = len(video_map)
        else:
            debug_metrics["video_error"] = f"Non-200 return payload: {vid_res.text[:120]}"
    except Exception as e:
        debug_metrics["video_status"] = "Exception Failed"
        debug_metrics["video_error"] = str(e)

    # 3. Structural mapping and pipeline parser matching
    try:
        actions = pbp_data.get("game", {}).get("actions", [])
        foul_idx = 1
        for act in actions:
            # We filter for actions tagged as fouls (typically code type 6)
            if act.get("actionType") == "foul" or "foul" in str(act.get("subType")).lower():
                event_id = str(act.get("actionNumber"))
                desc = act.get("description", "")
                
                # Assign video URL if matched from CDN mapping, otherwise use generic broadcast stream fallback
                final_video_url = video_map.get(event_id, f"https://www.nba.com/stats/events/?GameEventID={event_id}&GameID={gid}")
                
                p_name, f_klass = parse_foul_details(desc)
                
                catalog.append({
                    "foul_number": foul_idx,
                    "quarter": act.get("period", 1),
                    "clock": act.get("clock", "00:00"),
                    "team": act.get("teamTricode", "Unknown"),
                    "player": p_name if p_name != "Unknown" else (act.get("playerNameI", "Unknown")),
                    "type": f_klass,
                    "description": desc,
                    "video_url": final_video_url
                })
                foul_idx += 1
    except Exception as e:
        debug_metrics["pbp_error"] = f"Parsing Engine breakdown: {str(e)}"

    return catalog, debug_metrics

# Trigger downstream analytics engine
live_catalog, debug_logs = fetch_unthrottled_cdn_catalog(game_id)

# --- VISUAL DEBUGGER INTERFACE BLOCK ---
st.sidebar.markdown("---")
with st.sidebar.expander("🛠️ Screen Network Debug Tools", expanded=True):
    st.write(f"**PBP Endpoint HTTP Status:** `{debug_logs['pbp_status']}`")
    st.write(f"**Video CDN HTTP Status:** `{debug_logs['video_status']}`")
    st.write(f"**Assets Extracted Map Count:** `{debug_logs['video_keys_found']}`")
    if debug_logs['pbp_error'] or debug_logs['video_error']:
        st.error("Error logs caught on backend streams!")
        if debug_logs['pbp_error']: st.caption(f"PBP Error: {debug_logs['pbp_error']}")
        if debug_logs['video_error']: st.caption(f"Video Error: {debug_logs['video_error']}")
    if debug_logs['raw_video_sample']:
        st.caption("Raw CDN Video Object Schema:")
        st.json(debug_logs['raw_video_sample'])

if not live_catalog:
    st.error("Could not fetch game data or video mapping streams. Check the Network Debug Tools window in the sidebar to see exactly what failed.")
else:
    teams = sorted(list(set(item["team"] for item in live_catalog)))
    players = sorted(list(set(item["player"] for item in live_catalog)))
    types = sorted(list(set(item["type"] for item in live_catalog)))

    st.sidebar.header("Filter Matrix Options")
    filter_team = st.sidebar.selectbox("Filter by Team", ["All Teams"] + teams)
    filter_player = st.sidebar.selectbox("Filter by Player Profile", ["All Players"] + players)
    filter_type = st.sidebar.selectbox("Filter by Foul Type", ["All Types"] + types)

    filtered_items = []
    for meta in live_catalog:
        if filter_team != "All Teams" and meta["team"] != filter_team: continue
        if filter_player != "All Players" and meta["player"] != filter_player: continue
        if filter_type != "All Types" and meta["type"] != filter_type: continue
        filtered_items.append(meta)

    st.metric(label="Matching Video Clips Located", value=len(filtered_items))

    # --- SEAMLESS PLAYLIST MODE ---
    st.sidebar.markdown("---")
    st.sidebar.header("📺 Seamless Playlist Mode")
    
    if filtered_items:
        urls_to_play = [item["video_url"] for item in filtered_items]
        
        if "video_index" not in st.session_state:
            st.session_state.video_index = 0
            
        if st.session_state.video_index >= len(urls_to_play):
            st.session_state.video_index = 0

        st.sidebar.write(f"Playing clip **{st.session_state.video_index + 1}** of **{len(urls_to_play)}**")
        
        st.sidebar.video(urls_to_play[st.session_state.video_index])
        
        col_prev, col_next = st.sidebar.columns(2)
        with col_prev:
            if st.button("⬅️ Previous"):
                st.session_state.video_index = (st.session_state.video_index - 1) % len(urls_to_play)
                st.rerun()
        with col_next:
            if st.button("Next ➡️"):
                st.session_state.video_index = (st.session_state.video_index + 1) % len(urls_to_play)
                st.rerun()

    st.markdown("### Individual Clip Index Breakdown")
    for entry in filtered_items:
        with st.container():
            col1, col2 = st.columns([2, 3])
            with col1:
                st.subheader(f"Foul #{entry['foul_number']} - Q{entry['quarter']} ({entry['clock']})")
                st.write(f"**Player:** {entry['player']} ({entry['team']})")
                st.write(f"**Classification:** {entry['type']}")
                st.caption(f"Raw Entry Log: `{entry['description']}`")
            with col2:
                st.video(entry["video_url"])
            st.markdown("---")
