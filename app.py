"""
NBA Video-Assisted Foul Cataloger
==================================
Fetches every foul from any NBA game's play-by-play and maps each one
to its direct .mp4 clip from the NBA CDN.

Architecture notes
------------------
- curl_cffi Session (persistent, process-level) → TLS fingerprint reuse
  across all requests; avoids per-thread handshake bursts that trigger Akamai.
- Wave firing: requests go out in small groups with a delay between each wave,
  staying under Akamai's burst detection threshold while remaining fast.
- Two-pass retry: after all first-pass waves complete, unresolved event IDs
  get one more attempt with a wider inter-request gap.
- Cache: st.cache_resource (process-level, survives reruns) + disk JSON backup.
  Only successes are cached — failures are always retried on the next load.
- Asyncio is run inside a dedicated worker thread so it never conflicts with
  Streamlit's own tornado event loop.
- sdur check: videoeventsasset responses with sdur==0 are rejected (no actual
  video content despite a valid-looking URL).
- Unified parser: classify_foul and extract_player_name share the same ordered
  rule list, so the split position always matches the matched rule — no
  desync between the two passes over the description string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import streamlit as st
import streamlit.components.v1 as components

# ---------------------------------------------------------------------------
# Logging — goes to console/server log, not cluttering the UI
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nba_foul_catalog")


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

IMPERSONATE   = "chrome120"   # curl_cffi TLS profile. Try "chrome124" if 403s increase.
DISK_CACHE    = "nba_video_cache.json"
CACHE_VERSION = 3             # bump whenever on-disk structure changes

# Wave-firing parameters
WAVE_SIZE     = 8     # requests per wave
WAVE_DELAY_S  = 0.45  # seconds between waves
JITTER_MAX_S  = 0.12  # per-request random jitter within a wave
RETRY_DELAY_S = 0.90  # inter-request gap in the retry pass

REQUEST_TIMEOUT    = 15   # seconds per request
PREWARM_MAX_AGE_S  = 600  # re-warm session if last warm was >10 min ago

STATS_HEADERS: dict[str, str] = {
    "Accept":             "application/json, text/plain, */*",
    "Accept-Language":    "en-US,en;q=0.9",
    "Cache-Control":      "no-cache",
    "Connection":         "keep-alive",
    "Origin":             "https://www.nba.com",
    "Pragma":             "no-cache",
    "Referer":            "https://www.nba.com/",
    "Sec-Fetch-Dest":     "empty",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Site":     "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua":          '"Google Chrome";v="120", "Chromium";v="120", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
}

CDN_HEADERS: dict[str, str] = {
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.nba.com",
    "Referer":         "https://www.nba.com/",
    "User-Agent":      STATS_HEADERS["User-Agent"],
}

# ── Foul classification rules ─────────────────────────────────────────────────
# ORDER MATTERS: most-specific patterns first.
# Each entry: (lowercase_keyword, display_type)
# DUAL PURPOSE: used for both classification AND name splitting.
# parse_foul() scans this list and uses the position of the FIRST matching
# keyword to split the player name — so the split always agrees with the type.
FOUL_RULES: list[tuple[str, str]] = [
    ("flagrant.foul.type2", "Flagrant Type 2"),
    ("flagrant type 2",     "Flagrant Type 2"),
    ("flagrant.foul.type1", "Flagrant Type 1"),
    ("flagrant type 1",     "Flagrant Type 1"),
    ("flagrant",            "Flagrant"),
    ("l.b.foul",            "Loose Ball"),
    ("loose ball",          "Loose Ball"),
    ("personal take foul",  "Take Foul"),
    ("take foul",           "Take Foul"),
    ("take.foul",           "Take Foul"),   # dotted variant e.g. "Take.Foul"
    (".take",               "Take Foul"),
    ("clear path",          "Clear Path"),
    ("away from play",      "Away From Play"),
    ("s.foul",              "Shooting"),
    ("shooting foul",       "Shooting"),
    ("shooting personal",   "Shooting"),    # "shooting personal foul" pattern
    ("shooting",            "Shooting"),    # bare "shooting" before "personal"
    ("off.foul",            "Offensive"),
    ("offensive foul",      "Offensive"),
    ("t.foul",              "Technical"),
    ("technical",           "Technical"),
    ("p.foul",              "Personal"),
    ("personal foul",       "Personal"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# FOUL PARSER
# ═══════════════════════════════════════════════════════════════════════════════

def parse_foul(description: str, fallback_name: str = "Unknown") -> tuple[str, str]:
    """
    Return (player_name, foul_type) from a play-by-play action description.

    A single pass over FOUL_RULES finds the earliest matching keyword.
    That keyword's position is used for both the type label AND the name split,
    so the two values are always consistent regardless of description format.
    """
    if not description:
        return fallback_name, "Personal"

    lower        = description.lower()
    best_pos     = len(description)
    best_type    = "Personal"

    for keyword, foul_type in FOUL_RULES:
        idx = lower.find(keyword)
        if idx != -1 and idx < best_pos:
            best_pos  = idx
            best_type = foul_type

    raw_name = description[:best_pos].strip().rstrip(".")
    player   = raw_name if raw_name else fallback_name
    return player, best_type


# ═══════════════════════════════════════════════════════════════════════════════
# DISK CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def _load_disk_cache() -> dict[str, Any]:
    if not os.path.exists(DISK_CACHE):
        return {"_version": CACHE_VERSION}
    try:
        with open(DISK_CACHE) as f:
            data = json.load(f)
        if "_version" not in data:
            data["_version"] = CACHE_VERSION
        return data
    except Exception as exc:
        log.warning("Disk cache unreadable, starting fresh: %s", exc)
        return {"_version": CACHE_VERSION}


def _save_disk_cache(store: dict[str, Any]) -> None:
    try:
        with open(DISK_CACHE, "w") as f:
            json.dump(store, f)
    except Exception as exc:
        log.warning("Disk cache write failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESS-LEVEL STATE  (session + video store)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def _get_app_state() -> dict[str, Any]:
    """
    Process-level singleton. Created once, shared across all sessions/reruns.
    Keys:
        video_store  — { game_id: { event_id: mp4_url } }
        session      — curl_cffi.requests.Session (persistent TLS pool)
        warmed_at    — float timestamp of last pre-warm (0 = never)
    """
    from curl_cffi import requests as cffi_requests

    disk        = _load_disk_cache()
    video_store = {k: v for k, v in disk.items() if not k.startswith("_")}
    session     = cffi_requests.Session(impersonate=IMPERSONATE)
    session.headers.update(STATS_HEADERS)

    log.info("App state initialised. Disk cache: %d game(s).", len(video_store))
    return {"video_store": video_store, "session": session, "warmed_at": 0.0}


def _prewarm_session(state: dict[str, Any]) -> None:
    """
    GET nba.com to acquire Akamai session cookies before stats.nba.com calls.
    Re-runs if the last warm was more than PREWARM_MAX_AGE_S seconds ago,
    so stale cookies don't accumulate on long-running processes.
    """
    import time
    now = time.monotonic()
    if now - state["warmed_at"] < PREWARM_MAX_AGE_S:
        return
    try:
        state["session"].get("https://www.nba.com/", headers=CDN_HEADERS, timeout=10)
        log.info("Session pre-warm complete.")
    except Exception as exc:
        log.warning("Pre-warm failed (non-fatal): %s", exc)
    finally:
        state["warmed_at"] = now


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULE / SCOREBOARD  (for the home page)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=120)
def fetch_games_for_date(game_date: date) -> list[dict]:
    """
    Fetch NBA games for *game_date* from stats.nba.com/stats/scoreboardv3.
    Returns a list of dicts with keys: game_id, home, away, status, home_score,
    away_score, game_time (ET string).
    Returns [] on any failure.
    """
    from curl_cffi import requests as cffi_requests

    date_str = game_date.strftime("%m/%d/%Y")
    url      = (
        f"https://stats.nba.com/stats/scoreboardv3"
        f"?GameDate={date_str}&LeagueID=00&DayOffset=0"
    )
    try:
        resp = cffi_requests.get(
            url, headers=STATS_HEADERS, impersonate=IMPERSONATE, timeout=15
        )
        if resp.status_code != 200:
            log.warning("Scoreboard HTTP %d for %s", resp.status_code, date_str)
            return []
        data  = resp.json()
        games = (
            data.get("scoreboard", {}).get("games", [])
        )
        out = []
        for g in games:
            home = g.get("homeTeam", {})
            away = g.get("awayTeam", {})
            out.append({
                "game_id":    g.get("gameId", ""),
                "home":       home.get("teamTricode", "?"),
                "home_name":  home.get("teamName", ""),
                "away":       away.get("teamTricode", "?"),
                "away_name":  away.get("teamName", ""),
                "status":     g.get("gameStatusText", ""),
                "home_score": home.get("score", ""),
                "away_score": away.get("score", ""),
            })
        return out
    except Exception as exc:
        log.warning("Scoreboard fetch error: %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# PLAY-BY-PLAY FETCH
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=300)
def fetch_foul_catalog(game_id: str) -> tuple[list[dict], dict]:
    """
    Fetch the play-by-play for *game_id* and return every foul action.
    Uses a short-lived curl_cffi session (safe inside cache_data).
    The persistent Session in _get_app_state() is reserved for video fetching.
    """
    from curl_cffi import requests as cffi_requests

    debug: dict[str, Any] = {
        "pbp_status": "Not attempted",
        "pbp_error":  None,
        "source":     None,
    }

    pbp_endpoints = [
        (
            f"https://stats.nba.com/stats/playbyplayv3"
            f"?GameID={game_id}&StartPeriod=0&EndPeriod=0",
            STATS_HEADERS,
            "stats.nba.com",
            lambda d: d.get("game", {}).get("actions", []),
        ),
        (
            f"https://cdn.nba.com/static/json/liveData/playbyplay"
            f"/playbyplay_{game_id}.json",
            CDN_HEADERS,
            "cdn.nba.com",
            lambda d: d.get("game", {}).get("actions", []),
        ),
    ]

    actions: list[dict] = []
    for url, headers, label, extract in pbp_endpoints:
        try:
            resp = cffi_requests.get(
                url, headers=headers, impersonate=IMPERSONATE, timeout=15
            )
            debug["pbp_status"] = resp.status_code
            if resp.status_code == 200:
                actions = extract(resp.json())
                debug["source"] = label
                log.info("PBP loaded from %s (%d actions).", label, len(actions))
                break
            debug["pbp_error"] = f"{label} → HTTP {resp.status_code}"
        except Exception as exc:
            debug["pbp_error"] = f"{label} → {exc}"
            log.warning("PBP fetch error (%s): %s", label, exc)

    catalog: list[dict] = []
    foul_idx = 1

    for act in actions:
        action_type = str(act.get("actionType", "")).lower()
        sub_type    = str(act.get("subType",    "")).lower()
        description = act.get("description", "")

        is_foul = (
            action_type == "foul"
            or "foul" in sub_type
            or "foul" in description.lower()
            or (action_type == "turnover" and "offensive" in sub_type)
        )
        if not is_foul:
            continue

        event_id = act.get("actionNumber")
        if event_id is None:
            continue

        raw_clock   = act.get("clock", "PT00M00.00S")
        clean_clock = (
            raw_clock.replace("PT", "").replace("M", ":").replace("S", "")
            .split(".")[0]
        )

        fallback          = act.get("playerNameI") or act.get("playerName") or "Unknown"
        player, foul_type = parse_foul(description, fallback)

        catalog.append({
            "foul_number": foul_idx,
            "event_id":    str(event_id),
            "quarter":     act.get("period", 1),
            "clock":       clean_clock,
            "team":        act.get("teamTricode", "?"),
            "player":      player,
            "type":        foul_type,
            "description": description,
        })
        foul_idx += 1

    log.info("Foul catalog built: %d fouls.", len(catalog))
    return catalog, debug


# ═══════════════════════════════════════════════════════════════════════════════
# VIDEO URL RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_mp4_url(response_json: dict) -> str | None:
    """
    Pull the best available video URL from a videoeventsasset response.
    Returns None if sdur == 0 (clip exists in index but has no video content).
    """
    rsets = response_json.get("resultSets", {})

    if isinstance(rsets, dict):
        video_urls = rsets.get("Meta", {}).get("videoUrls", [])
    elif isinstance(rsets, list):
        video_urls = []
        for rs in rsets:
            if "videoUrls" in rs:
                video_urls = rs["videoUrls"]
                break
    else:
        return None

    for entry in video_urls:
        # sdur is duration in milliseconds. Zero means the clip has no content.
        if int(entry.get("sdur", 1)) == 0:
            log.debug("Skipping zero-duration clip entry.")
            continue
        for key in ("lurl", "murl", "surl", "hdurl", "sdurl", "vurl"):
            val = entry.get(key, "")
            if val and (".mp4" in val.lower() or ".m3u8" in val.lower()):
                return val
    return None


async def _fetch_one_video_async(
    session: Any,
    game_id: str,
    event_id: str,
    jitter: float = 0.0,
) -> tuple[str, str | None]:
    """
    Async wrapper. Runs the blocking curl_cffi call in the default executor
    so the event loop stays free for wave coordination.
    Returns (event_id, mp4_url_or_None).
    """
    if jitter > 0:
        await asyncio.sleep(jitter)

    url  = (
        f"https://stats.nba.com/stats/videoeventsasset"
        f"?GameEventID={event_id}&GameID={game_id}"
    )
    loop = asyncio.get_running_loop()

    def _blocking_get() -> tuple[str, str | None]:
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                mp4 = _extract_mp4_url(resp.json())
                if mp4:
                    return event_id, mp4
            elif resp.status_code == 429:
                log.warning("Rate-limited on event %s.", event_id)
        except Exception as exc:
            log.debug("Event %s fetch error: %s", event_id, exc)
        return event_id, None

    return await loop.run_in_executor(None, _blocking_get)


async def _fetch_all_waves(
    session: Any,
    game_id: str,
    event_ids: list[str],
    wave_size: int    = WAVE_SIZE,
    wave_delay: float = WAVE_DELAY_S,
    jitter_max: float = JITTER_MAX_S,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for start in range(0, len(event_ids), wave_size):
        wave  = event_ids[start : start + wave_size]
        tasks = [
            _fetch_one_video_async(
                session, game_id, eid,
                jitter=random.uniform(0, jitter_max),
            )
            for eid in wave
        ]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                continue
            eid, url = result
            if url:
                resolved[eid] = url
        if start + wave_size < len(event_ids):
            await asyncio.sleep(wave_delay)
    return resolved


async def _fetch_with_retry(
    session: Any,
    game_id: str,
    event_ids: list[str],
) -> dict[str, str]:
    """
    Pass 1 — wave-fire all event_ids.
    Pass 2 — retry failures with smaller waves and longer gaps.
    """
    log.info("Pass 1: %d events, waves of %d.", len(event_ids), WAVE_SIZE)
    resolved = await _fetch_all_waves(session, game_id, event_ids)
    log.info("Pass 1: %d/%d resolved.", len(resolved), len(event_ids))

    still_missing = [e for e in event_ids if e not in resolved]
    if still_missing:
        log.info("Pass 2: retrying %d events.", len(still_missing))
        retry = await _fetch_all_waves(
            session, game_id, still_missing,
            wave_size=3, wave_delay=RETRY_DELAY_S, jitter_max=0.2,
        )
        resolved.update(retry)
        log.info("Pass 2: +%d. Total: %d/%d.", len(retry), len(resolved), len(event_ids))

    return resolved


def _run_async_in_thread(coro) -> Any:
    """Run a coroutine in a dedicated thread with its own event loop."""
    def _worker():
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker).result()


def fetch_missing_videos(
    game_id: str,
    event_ids: list[str],
    state: dict[str, Any],
) -> int:
    """
    Resolve video URLs for any event_ids not already in the store.
    Only successes are stored — failures are retried on the next call.
    Returns the number of newly resolved URLs.
    """
    video_store = state["video_store"]
    game_store  = video_store.setdefault(game_id, {})
    missing     = [e for e in event_ids if e not in game_store]

    if not missing:
        return 0

    log.info("Resolving %d missing URLs for game %s.", len(missing), game_id)
    newly = _run_async_in_thread(_fetch_with_retry(state["session"], game_id, missing))

    if newly:
        game_store.update(newly)
        disk_data = {"_version": CACHE_VERSION}
        disk_data.update(video_store)
        _save_disk_cache(disk_data)
        log.info("Stored %d new URLs to disk.", len(newly))

    return len(newly)


# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def fallback_link(game_id: str, entry: dict) -> str:
    enc = quote(entry["description"])
    return (
        f"https://www.nba.com/stats/events"
        f"?CFID=&CFPARAMS=&GameEventID={entry['event_id']}"
        f"&GameID={game_id}&Season=2025-26&flag=1&title={enc}"
    )


def _resolve_status_text(hit: int, total: int) -> str:
    if total == 0:
        return "No fouls"
    pct  = int(100 * hit / total)
    icon = "✅" if hit == total else ("⚠️" if hit > 0 else "❌")
    return f"{icon} {hit}/{total} videos resolved ({pct}%)"


def _autoplay_player(clips: list[dict], game_store: dict) -> None:
    """
    Render a self-advancing HTML5 video player that cycles through all
    available clips in *clips* automatically on ended, with skip controls.
    clips: list of foul dicts. game_store: event_id -> mp4_url.
    """
    urls   = []
    labels = []
    for c in clips:
        url = game_store.get(c["event_id"])
        if url:
            urls.append(url)
            labels.append(
                f"#{c['foul_number']} — {c['player']} ({c['team']}) {c['type']} "
                f"Q{c['quarter']} {c['clock']}"
            )

    if not urls:
        st.warning("No video clips available for the current filter.")
        return

    urls_js   = json.dumps(urls)
    labels_js = json.dumps(labels)

    # SWAPPED TO SINGLE TRIPLE QUOTES HERE TO BREAK THE METADATA PARSING LOCK
    html = f'''
<style>
  body {{ margin:0; background:#0a0a0a; }}
  #wrap {{ position:relative; width:100%; background:#000; border-radius:8px; overflow:hidden; }}
  #player {{ width:100%; display:block; max-height:480px; background:#000; }}
  #bar {{
    display:flex; align-items:center; gap:10px; padding:10px 14px;
    background:#111; font-family:system-ui,sans-serif; color:#eee; font-size:13px;
  }}
  #label {{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  button {{
    background:#1d428a; color:#fff; border:none; border-radius:5px;
    padding:5px 14px; cursor:pointer; font-size:13px; font-weight:600;
  }}
  button:hover {{ background:#c8102e; }}
  #counter {{ color:#aaa; white-space:nowrap; }}
</style>
<div id="wrap">
  <video id="player" src="{urls[0]}" autoplay controls playsinline></video>
  <div id="bar">
    <button onclick="go(-1)">&#9664; Prev</button>
    <span id="label">{labels[0]}</span>
    <span id="counter">1 / {len(urls)}</span>
    <button onclick="go(1)">Next &#9654;</button>
  </div>
</div>
<script>
const clips  = {urls_js};
const labels = {labels_js};
let idx = 0;
const v   = document.getElementById('player');
const lbl = document.getElementById('label');
const ctr = document.getElementById('counter');

function show(i) {{
  idx = ((i % clips.length) + clips.length) % clips.length;
  v.src = clips[idx];
  lbl.textContent = labels[idx];
  ctr.textContent = (idx + 1) + ' / ' + clips.length;
  v.play();
}}
v.addEventListener('ended', () => show(idx + 1));
function go(d) {{ show(idx + d); }}
</script>
'''
    components.html(html, height=560)


# ═══════════════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════════════

def _page_home() -> None:
    """Home page: date picker + game browser."""
    st.title("🏀 NBA Foul Catalog")
    st.markdown("Pick a date to browse games, then click a game to load its foul reel.")

    today     = date.today()
    col_d, col_s = st.columns([2, 3])
    with col_d:
        chosen_date = st.date_input(
            "Game date",
            value=today,
            min_value=date(2000, 1, 1),
            max_value=today,
        )
    with col_s:
        manual_id = st.text_input(
            "Or enter a Game ID directly",
            placeholder="e.g. 0042500403",
        )
        if manual_id.strip():
            if st.button("Load game →", type="primary"):
                st.session_state.game_id   = manual_id.strip()
                st.session_state.page      = "catalog"
                st.rerun()

    st.markdown("---")

    with st.spinner(f"Fetching games for {chosen_date.strftime('%B %d, %Y')}…"):
        games = fetch_games_for_date(chosen_date)

    if not games:
        st.info(
            "No games found for that date, or the schedule couldn't be reached. "
            "Try a different date or enter a Game ID directly above."
        )
        return

    st.subheader(f"{chosen_date.strftime('%A, %B %d, %Y')} — {len(games)} game(s)")

    cols = st.columns(min(len(games), 3))
    for i, g in enumerate(games):
        with cols[i % 3]:
            score_line = (
                f"**{g['away']} {g['away_score']}  —  {g['home_score']} {g['home']}**"
                if g["away_score"] != ""
                else f"**{g['away']} @ {g['home']}**"
            )
            st.markdown(score_line)
            st.caption(g["status"])
            if st.button(f"Load fouls →", key=f"game_{g['game_id']}"):
                st.session_state.game_id = g["game_id"]
                st.session_state.page    = "catalog"
                st.rerun()
            st.markdown("&nbsp;")


def _page_catalog(game_id: str) -> None:
    """Foul catalog page for a specific game."""

    # ── Back button ────────────────────────────────────────────────────────────
    if st.sidebar.button("← Back to schedule"):
        st.session_state.page = "home"
        st.rerun()

    st.sidebar.markdown("---")

    # ── Load play-by-play ──────────────────────────────────────────────────────
    with st.spinner("Loading play-by-play…"):
        catalog, debug = fetch_foul_catalog(game_id)

    with st.sidebar.expander("🛠 Network debug", expanded=False):
        st.write(f"**PBP status:** `{debug['pbp_status']}`")
        st.write(f"**Source:** `{debug.get('source') or '—'}`")
        if debug["pbp_error"]:
            st.error(debug["pbp_error"])

    if not catalog:
        st.error(
            "Could not fetch play-by-play data. "
            "Check the Game ID and the Network debug panel."
        )
        st.stop()

    # ── Filters ────────────────────────────────────────────────────────────────
    all_teams   = sorted({e["team"]   for e in catalog})
    all_players = sorted({e["player"] for e in catalog})
    all_types   = sorted({e["type"]   for e in catalog})

    st.sidebar.header("Filter")
    filter_team   = st.sidebar.selectbox("Team",   ["All teams"]   + all_teams)
    filter_player = st.sidebar.selectbox("Player", ["All players"] + all_players)
    filter_type   = st.sidebar.selectbox("Type",   ["All types"]   + all_types)

    is_filtered = (
        filter_team   != "All teams"
        or filter_player != "All players"
        or filter_type   != "All types"
    )

    filtered = [
        e for e in catalog
        if (filter_team   == "All teams"   or e["team"]   == filter_team)
        and (filter_player == "All players" or e["player"] == filter_player)
        and (filter_type   == "All types"   or e["type"]   == filter_type)
    ]

    if not filtered:
        st.info("No fouls match the current filters.")
        st.stop()

    # ── Resolve video URLs ─────────────────────────────────────────────────────
    state       = _get_app_state()
    _prewarm_session(state)
    video_store = state["video_store"]
    all_eids    = [e["event_id"] for e in catalog]
    game_store  = video_store.get(game_id, {})
    missing_ct  = sum(1 for eid in all_eids if eid not in game_store)

    if missing_ct:
        with st.spinner(f"Fetching {missing_ct} clip URL(s)…"):
            fetch_missing_videos(game_id, all_eids, state)
        game_store = video_store.get(game_id, {})

    hit  = sum(1 for eid in all_eids if game_store.get(eid))
    miss = len(all_eids) - hit

    col_title, col_stat, col_retry = st.columns([3, 3, 1])
    with col_title:
        st.title(f"🏀 Game {game_id}")
    with col_stat:
        st.caption(_resolve_status_text(hit, len(all_eids)))
        st.caption(f"Showing {len(filtered)} / {len(catalog)} fouls")
    with col_retry:
        if miss > 0 and st.button("🔄 Retry"):
            st.rerun()

    # ── Autoplay reel (when filtered or when user clicks Play All) ─────────────
    show_reel = is_filtered
    if not is_filtered:
        show_reel = st.toggle("▶ Play all fouls as continuous reel", value=False)

    if show_reel:
        st.markdown("---")
        _autoplay_player(filtered, game_store)
        st.markdown("---")

    # ── Full clip index ────────────────────────────────────────────────────────
    st.markdown(f"### Clip index — {len(filtered)} foul(s)")

    for entry in filtered:
        clip_url = game_store.get(entry["event_id"])
        with st.container():
            meta_col, video_col = st.columns([2, 3])
            with meta_col:
                st.subheader(
                    f"#{entry['foul_number']} — Q{entry['quarter']} ({entry['clock']})"
                )
                st.write(f"**Player:** {entry['player']} ({entry['team']})")
                st.write(f"**Type:** {entry['type']}")
                st.caption(f"`{entry['description']}`")
            with video_col:
                if clip_url:
                    st.video(clip_url)
                else:
                    st.warning("Clip unavailable.")
                    st.markdown(f"[Open on NBA.com ↗]({fallback_link(game_id, entry)})")
        st.markdown("---")


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    st.set_page_config(
        page_title="NBA Foul Catalog",
        page_icon="🏀",
        layout="wide",
    )

    if "page" not in st.session_state:
        st.session_state.page    = "home"
        st.session_state.game_id = ""

    if st.session_state.page == "catalog" and st.session_state.game_id:
        _page_catalog(st.session_state.game_id)
    else:
        _page_home()


if __name__ == "__main__":
    main()
