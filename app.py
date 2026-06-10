import os
import json
import re
import streamlit as st
from curl_cffi import requests

st.set_page_config(page_title="NBA Live Foul Catalog", layout="wide")
st.title("🏀 NBA Live Search & Play Catalog")

game_id = st.sidebar.text_input("NBA Game ID Input", value="0042500403")

# An exact 1:1 clone of your browser's network request to bypass the WAF
CHROME_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.nba.com",
    "Pragma": "no-cache",
    "Referer": "https://www.nba.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"'
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

@st.cache_data(show_spinner=False)
def get_resolved_mp4_url(gid, event_id):
    """
    Queries the backend video API securely using the exact browser network profile.
    """
    asset_url = f"https://stats.nba.com/stats/videoeventsasset?GameEventID={event_id}&GameID={gid}"
    try:
        # Dropped the impersonate flag here to let your exact raw headers do the talking
        res = requests.get(asset_url, headers=CHROME_HEADERS, timeout=8)
        if res.status_code == 200:
            data = res.json()
            rsets = data.get("resultSets", {})
            video_urls = []
            
            if isinstance(rsets, dict):
                video_urls = rsets.get("Meta", {}).get("videoUrls", [])
            elif isinstance(rsets, list):
                for rset in rsets:
                    if rset.get("name") == "Meta" or "videoUrls" in rset:
                        video_urls = rset.get("videoUrls", [])
                        break
                        
            if video_urls and isinstance(video_urls, list):
                v = video_urls[0]
                direct_mp4 = v.get("lurl") or v.get("murl") or v.get("hdurl") or v.get("sdurl") or v.get("vurl")
                if direct_mp4 and (".mp4" in direct_mp4.lower() or ".m3u8" in direct_mp4.lower()):
                    return direct_mp4
    except Exception:
        pass
    return None

def fetch_unthrottled_cdn_catalog(gid):
    debug_metrics = {
        "pbp_status": "Not Attempted",
        "video_status": "Exact cURL Header Mapping",
        "pbp_error": None
    }
    catalog = []
    pbp_url = f"https://stats.nba.com/stats/playbyplayv3?GameID={gid}&StartPeriod=0&EndPeriod=0"
    
    try:
        pbp_res = requests.get(pbp_url, headers=CHROME_HEADERS, timeout=15)
        debug_metrics["pbp_status"] = pbp_res.status_code
        if pbp_res.status_code != 200:
            debug_metrics["pbp_error"] = f"Non-200 return code: {pbp_res.status_code}"
            return catalog, debug_metrics
        pbp_data = pbp_res.json()
    except Exception as e:
        debug_metrics["pbp_status"] = "Exception Failed"
        debug_metrics["pbp_error"] = str(e)
        return catalog, debug_metrics

    try:
        actions = pbp_data.get("game", {}).get("actions", [])
        foul_idx = 1
        for act in actions:
            action_type = str(act.get("actionType", "")).lower()
            sub_type = str(act.get("subType", "")).lower()
            desc = act.get("description", "")
            
            is_foul = (action_type == "foul") or \
                       ("foul" in sub_type) or \
                       (action_type == "turnover" and "offensive" in sub_type) or \
                       ("foul" in desc.lower())
                       
            if is_foul:
                event_id = str(act.get("actionNumber"))
                raw_clock = act.get("clock", "00:00")
                clean_clock = raw_clock.replace("PT", "").replace("M", ":").replace("S", "")
                if "." in clean_clock:
                    clean_clock = clean_clock.split(".")[0]
                
                p_name, f_klass = parse_foul_details(desc)
                
                catalog.append({
                    "foul_number": foul_idx,
                    "event_id": event_id,
                    "quarter": act.get("period", 1),
                    "clock": clean_clock,
                    "team": act.get("teamTricode", "Unknown"),
                    "player": p_name if p_name != "Unknown" else (act.get("playerNameI", "Unknown")),
                    "type": f_klass,
                    "description": desc
                })
                foul_idx += 1
    except Exception as e:
        debug_metrics["pbp_error"] = f"Parsing Engine breakdown: {str(e)}"

    return catalog, debug_metrics


# --- RUNTIME PIPELINE STARTS ---

live_catalog, debug_logs = fetch_unthrottled_cdn_catalog(game_id)

# --- VISUAL DEBUGGER INTERFACE BLOCK ---
st.sidebar.markdown("---")
with st.sidebar.expander("🛠️ Screen Network Debug Tools", expanded=True):
    st.write(f"**PBP Endpoint HTTP Status:** `{debug_logs['pbp_status']}`")
    st.write(f"**Video Architecture:** `{debug_logs['video_status']}`")
    if debug_logs['pbp_error']:
        st.error(f"PBP Error caught: {debug_logs['pbp_error']}")

if not live_catalog:
    st.error("Could not fetch game data structural streams. Check the Network Debug Tools.")
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
        if "video_index" not in st.session_state:
            st.session_state.video_index = 0
            
        if st.session_state.video_index >= len(filtered_items):
            st.session_state.video_index = 0

        st.sidebar.write(f"Playing clip **{st.session_state.video_index + 1}** of **{len(filtered_items)}**")
        
        active_item = filtered_items[st.session_state.video_index]
        active_url = get_resolved_mp4_url(game_id, active_item["event_id"])
        
        if active_url:
            st.sidebar.video(active_url)
        else:
            encoded_title = requests.utils.quote(active_item["description"])
            fallback_link = f"https://www.nba.com/stats/events?CFID=&CFPARAMS=&GameEventID={active_item['event_id']}&GameID={game_id}&Season=2025-26&flag=1&title={encoded_title}"
            st.sidebar.warning("📺 Play asset unavailable for direct player embedding.")
            st.sidebar.markdown(f"[🔗 View Play on NBA Official Site]({fallback_link})")
        
        col_prev, col_next = st.sidebar.columns(2)
        with col_prev:
            if st.button("⬅️ Previous"):
                st.session_state.video_index = (st.session_state.video_index - 1) % len(filtered_items)
                st.rerun()
        with col_next:
            if st.button("Next ➡️"):
                st.session_state.video_index = (st.session_state.video_index + 1) % len(filtered_items)
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
                clip_url = get_resolved_mp4_url(game_id, entry["event_id"])
                
                if clip_url:
                    st.video(clip_url)
                else:
                    encoded_title = requests.utils.quote(entry["description"])
                    fallback_link = f"https://www.nba.com/stats/events?CFID=&CFPARAMS=&GameEventID={entry['event_id']}&GameID={game_id}&Season=2025-26&flag=1&title={encoded_title}"
                    st.warning("📺 Video streaming link not compiled by server API.")
                    st.markdown(f"[🔗 View Play on NBA Official Site]({fallback_link})")
            st.markdown("---")
