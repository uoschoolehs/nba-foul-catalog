import os
import json
import re
import streamlit as st
from curl_cffi import requests

st.set_page_config(page_title="NBA Live Foul Catalog", layout="wide")
st.title("🏀 NBA Live Search & Play Catalog")

# Input field so users can change the game ID dynamically on the live site
game_id = st.sidebar.text_input("NBA Game ID Input", value="0042500403")

CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com"
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
        if word.lower() in split_keywords or word.upper() in ["FOUL", "L.B.FOUL"]:
            break
        player_words.append(word)
        
    player_name = " ".join(player_words).strip() if player_words else "Unknown"
    return player_name, foul_type

@st.cache_data(ttl=600)
def fetch_unthrottled_cdn_catalog(g_id):
    """
    Simultaneously pulls play-by-play text data and real-time video hashes 
    directly via public CDN endpoints to bypass strict rate-limiting firewalls.
    """
    pbp_url = f"https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{g_id}.json"
    video_cdn_url = f"https://cdn.nba.com/static/json/liveData/video/video_{g_id}.json"
    
    # 1. Fetch official asset map from the unthrottled CDN
    video_map = {}
    try:
        vid_res = requests.get(video_cdn_url, headers=CHROME_HEADERS, impersonate="chrome", timeout=10)
        if vid_res.status_code == 200:
            video_events = vid_res.json().get("video", {}).get("videoEvents", [])
            for event in video_events:
                ev_id = str(event.get("ei"))  # Event ID key
                asset_id = str(event.get("uuid"))  # True video string asset key
                if ev_id and asset_id:
                    video_map[ev_id] = asset_id
    except Exception:
        pass

    # 2. Fetch standard play-by-play timeline log
    try:
        pbp_res = requests.get(pbp_url, headers=CHROME_HEADERS, impersonate="chrome", timeout=10)
        if pbp_res.status_code != 200:
            return None
        actions = pbp_res.json().get("game", {}).get("actions", [])
    except Exception:
        return None

    # 3. Compile them instantly using the downloaded asset strings
    catalog = []
    foul_idx = 0
    for action in actions:
        action_type = action.get("actionType", "")
        desc = action.get("description", "")
        if action_type.lower() == "foul" or "foul" in desc.lower():
            foul_idx += 1
            event_id = str(action.get("actionNumber"))
            
            # Extract the actual unique file signature from our CDN dictionary mapping
            asset_uuid = video_map.get(event_id)
            if not asset_uuid:
                continue  # Filter out tracking numbers missing official video tracks
                
            player, f_type = parse_foul_details(desc)
            stream_url = f"https://videos.nba.com/nba/pbp/media/{g_id}/{event_id}/{asset_uuid}_1280x720.mp4"
            
            catalog.append({
                "foul_number": foul_idx,
                "event_id": event_id,
                "team": action.get("teamTricode", "UNKNOWN"),
                "player": player,
                "type": f_type,
                "description": desc,
                "clock": action.get("clock", "00:00"),
                "quarter": action.get("period", 1),
                "video_url": stream_url
            })
    return catalog

live_catalog = fetch_unthrottled_cdn_catalog(game_id)

if not live_catalog:
    st.error("Could not fetch game data or video mapping streams. Double check your Game ID configuration.")
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
