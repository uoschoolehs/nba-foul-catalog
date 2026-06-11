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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import streamlit as st

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

# curl_cffi TLS profile — impersonates Chrome 120's full TLS fingerprint.
# Try "chrome124" if 403-rate increases; the profile list is in curl_cffi docs.
IMPERSONATE = "chrome120"

# Disk cache path. Relative to app.py's working directory.
# Contains all previously resolved game → event → mp4 mappings.
DISK_CACHE = "nba_video_cache.json"
CACHE_VERSION = 2  # bump if the on-disk structure changes

# Wave-firing parameters (the core anti-Akamai-burst strategy)
WAVE_SIZE       = 8      # requests per wave
WAVE_DELAY_S    = 0.45   # seconds between waves
JITTER_MAX_S    = 0.12   # per-request random jitter within a wave (±)
RETRY_DELAY_S   = 0.90   # inter-request delay for the second-pass retry

# Per-request timeout (seconds). 15s matches the prior code.
REQUEST_TIMEOUT = 15

# Headers for stats.nba.com (requires x-nba-stats-* headers or returns 403)
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
    "User-Agent":         (
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

# Headers for the CDN fallback (no x-nba-stats-* needed)
CDN_HEADERS: dict[str, str] = {
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.nba.com",
    "Referer":         "https://www.nba.com/",
    "User-Agent":      STATS_HEADERS["User-Agent"],
}

# Known foul description keywords, longest/most-specific first so the
# classifier doesn't short-circuit on a substring match.
_FOUL_TYPE_RULES: list[tuple[str, str]] = [
    ("flagrant.foul.type2",  "Flagrant Type 2"),
    ("flagrant type 2",      "Flagrant Type 2"),
    ("flagrant.foul.type1",  "Flagrant Type 1"),
    ("flagrant type 1",      "Flagrant Type 1"),
    ("flagrant",             "Flagrant"),
    ("l.b.foul",             "Loose Ball"),
    ("loose ball",           "Loose Ball"),
    ("personal take foul",   "Take Foul"),
    ("take foul",            "Take Foul"),
    (".take",                "Take Foul"),
    ("clear path",           "Clear Path"),
    ("away from play",       "Away From Play"),
    ("s.foul",               "Shooting"),
    ("shooting foul",        "Shooting"),
    ("off.foul",             "Offensive"),
    ("offensive foul",       "Offensive"),
    ("t.foul",               "Technical"),
    ("technical",            "Technical"),
    ("p.foul",               "Personal"),
    ("personal foul",        "Personal"),
]

_FOUL_SPLIT_KEYWORDS: list[str] = [
    "flagrant", "l.b.foul", "loose", "personal", "s.foul", "p.foul",
    "off.foul", "t.foul", "take foul", "clear path", "away from play",
    "shooting", "offensive", "technical", "foul",
]


# ═══════════════════════════════════════════════════════════════════════════════
# FOUL PARSER  (logic unchanged from handover; structure cleaned up)
# ═══════════════════════════════════════════════════════════════════════════════

def classify_foul(description: str) -> str:
    """Return the foul type string for a play-by-play description."""
    lower = description.lower()
    for keyword, foul_type in _FOUL_TYPE_RULES:
        if keyword in lower:
            return foul_type
    return "Personal"


def extract_player_name(description: str, fallback: str = "Unknown") -> str:
    """
    Extract the player name from a foul description by finding where
    the foul keyword begins and taking everything before it.
    """
    lower = description.lower()
    split_pos = len(description)
    for kw in _FOUL_SPLIT_KEYWORDS:
        idx = lower.find(kw)
        if idx != -1 and idx < split_pos:
            split_pos = idx
    raw = description[:split_pos].strip().rstrip(".")
    return raw if raw else fallback


def parse_foul(description: str, fallback_name: str = "Unknown") -> tuple[str, str]:
    """Return (player_name, foul_type) from a play-by-play action description."""
    if not description:
        return fallback_name, "Personal"
    return extract_player_name(description, fallback_name), classify_foul(description)


# ═══════════════════════════════════════════════════════════════════════════════
# DISK CACHE  (JSON, keyed by game_id → event_id → mp4_url)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_disk_cache() -> dict[str, Any]:
    if not os.path.exists(DISK_CACHE):
        return {"_version": CACHE_VERSION}
    try:
        with open(DISK_CACHE) as f:
            data = json.load(f)
        # Migrate old format (no _version key) transparently
        if "_version" not in data:
            data["_version"] = CACHE_VERSION
        return data
    except Exception as exc:
        log.warning("Disk cache unreadable, starting fresh: %s", exc)
        return {"_version": CACHE_VERSION}


def _save_disk_cache(store: dict[str, Any]) -> None:
    """Best-effort write — never raises so a disk error never crashes the app."""
    try:
        with open(DISK_CACHE, "w") as f:
            json.dump(store, f)
    except Exception as exc:
        log.warning("Disk cache write failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESS-LEVEL SESSION + CACHE  (st.cache_resource → one instance per process)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def _get_app_state() -> dict[str, Any]:
    """
    Returns the single process-level state dict, shared across all sessions
    and reruns.  Populated once from disk on first call.

    Keys:
        "video_store"  — { game_id: { event_id: mp4_url } }
        "session"      — curl_cffi.requests.Session (persistent connection pool)
        "warmed"       — bool, whether the pre-warm request has been made
    """
    from curl_cffi import requests as cffi_requests

    disk = _load_disk_cache()
    # Strip meta keys so only game-ID dicts remain in video_store
    video_store = {k: v for k, v in disk.items() if not k.startswith("_")}

    session = cffi_requests.Session(impersonate=IMPERSONATE)
    session.headers.update(STATS_HEADERS)

    log.info(
        "App state initialised. Disk cache loaded %d game(s).",
        len(video_store),
    )
    return {
        "video_store": video_store,
        "session":     session,
        "warmed":      False,
    }


def _prewarm_session(state: dict[str, Any]) -> None:
    """
    Make one GET request to nba.com so Akamai sets its session cookie
    before any stats.nba.com calls go out.  Runs at most once per process.
    """
    if state["warmed"]:
        return
    try:
        state["session"].get(
            "https://www.nba.com/",
            headers=CDN_HEADERS,
            timeout=10,
        )
        log.info("Session pre-warm complete.")
    except Exception as exc:
        log.warning("Pre-warm failed (non-fatal): %s", exc)
    finally:
        state["warmed"] = True  # don't retry even on failure


# ═══════════════════════════════════════════════════════════════════════════════
# PLAY-BY-PLAY FETCH
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=300)
def fetch_foul_catalog(game_id: str) -> tuple[list[dict], dict]:
    """
    Fetch the play-by-play for *game_id* and return every foul action.

    Uses its own short-lived curl_cffi session (safe inside cache_data).
    The persistent process-level session in _get_app_state() is reserved
    for video URL resolution, which happens outside of cache_data.

    Returns
    -------
    catalog : list[dict]
        One dict per foul with keys: foul_number, event_id, quarter, clock,
        team, player, type, description.
    debug   : dict
        Diagnostic info for the sidebar debug panel.
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
            "stats.nba.com/playbyplayv3",
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
        )
        clean_clock = clean_clock.split(".")[0]

        fallback    = act.get("playerNameI") or act.get("playerName") or "Unknown"
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
# VIDEO URL RESOLUTION  (the fixed bottleneck)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_mp4_url(response_json: dict) -> str | None:
    """
    Pull the best available video URL out of a videoeventsasset response.
    Preference order: lurl (1280p) > murl (960p) > surl (320p) > any mp4/m3u8.
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
    Async wrapper around the synchronous curl_cffi request.
    Runs the blocking call in the default executor so the event loop stays free.

    Returns (event_id, mp4_url_or_None).
    """
    if jitter > 0:
        await asyncio.sleep(jitter)

    url = (
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
    """
    Fire event_ids in waves of *wave_size*, with *wave_delay* seconds between
    each wave and up to *jitter_max* seconds of per-request random jitter.

    Returns a dict of { event_id: mp4_url } for successfully resolved events.
    """
    resolved: dict[str, str] = {}

    for wave_start in range(0, len(event_ids), wave_size):
        wave = event_ids[wave_start : wave_start + wave_size]
        tasks = [
            _fetch_one_video_async(
                session, game_id, eid,
                jitter=random.uniform(0, jitter_max),
            )
            for eid in wave
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            eid, url = result
            if url:
                resolved[eid] = url

        if wave_start + wave_size < len(event_ids):
            await asyncio.sleep(wave_delay)

    return resolved


async def _fetch_with_retry(
    session: Any,
    game_id: str,
    event_ids: list[str],
) -> dict[str, str]:
    """
    Two-pass strategy:
      Pass 1 — wave-fire all event_ids (fast, WAVE_SIZE at a time).
      Pass 2 — retry every unresolved event_id sequentially with RETRY_DELAY_S
               between each request (gentle, avoids re-triggering burst limits).
    """
    # Pass 1 — wave firing
    log.info(
        "Pass 1: %d events, waves of %d, %.2fs gap.",
        len(event_ids), WAVE_SIZE, WAVE_DELAY_S,
    )
    resolved = await _fetch_all_waves(session, game_id, event_ids)
    log.info("Pass 1 complete: %d/%d resolved.", len(resolved), len(event_ids))

    # Pass 2 — sequential retry of failures
    still_missing = [eid for eid in event_ids if eid not in resolved]
    if still_missing:
        log.info("Pass 2: retrying %d unresolved events.", len(still_missing))
        retry_resolved = await _fetch_all_waves(
            session, game_id, still_missing,
            wave_size=3,           # smaller waves for the retry pass
            wave_delay=RETRY_DELAY_S,
            jitter_max=0.2,
        )
        resolved.update(retry_resolved)
        log.info(
            "Pass 2 complete: %d additional resolved. Total: %d/%d.",
            len(retry_resolved), len(resolved), len(event_ids),
        )

    return resolved


def _run_async_in_thread(coro) -> Any:
    """
    Run an async coroutine in a dedicated thread with its own event loop.
    This avoids conflicts with Streamlit's own tornado event loop on the
    main thread.
    """
    def _worker():
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker).result()


def fetch_missing_videos(
    game_id: str,
    event_ids: list[str],
    state: dict[str, Any],
    progress_callback=None,
) -> int:
    """
    Resolve video URLs for any *event_ids* not already in the video store.
    Updates the process-level store and writes to disk on any new finds.

    Returns the number of newly resolved URLs.
    """
    video_store = state["video_store"]
    game_store  = video_store.setdefault(game_id, {})
    missing     = [eid for eid in event_ids if eid not in game_store]

    if not missing:
        return 0

    log.info("Resolving %d missing video URLs for game %s.", len(missing), game_id)

    if progress_callback:
        progress_callback(0, len(missing), 0)

    session = state["session"]

    newly_resolved: dict[str, str] = _run_async_in_thread(
        _fetch_with_retry(session, game_id, missing)
    )

    if newly_resolved:
        game_store.update(newly_resolved)
        # Persist: build a dict that mirrors disk format (strip internal keys)
        disk_data = {"_version": CACHE_VERSION}
        disk_data.update(video_store)
        _save_disk_cache(disk_data)
        log.info("Wrote %d new URLs to disk cache.", len(newly_resolved))

    if progress_callback:
        progress_callback(len(missing), len(missing), len(newly_resolved))

    return len(newly_resolved)


# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def fallback_link(game_id: str, entry: dict) -> str:
    """NBA stats event page URL for when the direct video link is unavailable."""
    enc = quote(entry["description"])
    return (
        f"https://www.nba.com/stats/events"
        f"?CFID=&CFPARAMS=&GameEventID={entry['event_id']}"
        f"&GameID={game_id}&Season=2025-26&flag=1&title={enc}"
    )


def _fmt_resolve_status(hit: int, total: int) -> str:
    pct = int(100 * hit / total) if total else 0
    icon = "✅" if hit == total else ("⚠️" if hit > 0 else "❌")
    return f"{icon} {hit}/{total} videos resolved ({pct}%)"


# ═══════════════════════════════════════════════════════════════════════════════
# STREAMLIT APP
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    st.set_page_config(page_title="NBA Foul Catalog", layout="wide")
    st.title("🏀 NBA Foul Catalog")

    # ── Sidebar: game input ───────────────────────────────────────────────────
    st.sidebar.header("Game")
    game_id = st.sidebar.text_input(
        "Game ID",
        value="0042500403",
        help="10-digit NBA game ID, e.g. 0042500403 for 2025-26 Finals Game 3.",
    )

    # ── Load play-by-play ─────────────────────────────────────────────────────
    with st.spinner("Loading play-by-play…"):
        catalog, debug = fetch_foul_catalog(game_id)

    # ── Debug panel ───────────────────────────────────────────────────────────
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

    # ── Filters ───────────────────────────────────────────────────────────────
    st.sidebar.header("Filter")
    all_teams   = sorted({e["team"]   for e in catalog})
    all_players = sorted({e["player"] for e in catalog})
    all_types   = sorted({e["type"]   for e in catalog})

    filter_team   = st.sidebar.selectbox("Team",   ["All teams"]   + all_teams)
    filter_player = st.sidebar.selectbox("Player", ["All players"] + all_players)
    filter_type   = st.sidebar.selectbox("Type",   ["All types"]   + all_types)

    filtered = [
        e for e in catalog
        if (filter_team   == "All teams"   or e["team"]   == filter_team)
        and (filter_player == "All players" or e["player"] == filter_player)
        and (filter_type   == "All types"   or e["type"]   == filter_type)
    ]

    st.metric("Fouls shown", len(filtered), delta=None)

    if not filtered:
        st.info("No fouls match the current filters.")
        st.stop()

    # ── Resolve video URLs ────────────────────────────────────────────────────
    state       = _get_app_state()
    _prewarm_session(state)        # acquire nba.com cookies before stats calls
    video_store = state["video_store"]
    all_eids    = [e["event_id"] for e in catalog]
    game_store  = video_store.get(game_id, {})
    missing_ct  = sum(1 for eid in all_eids if eid not in game_store)

    if missing_ct:
        progress_bar  = st.progress(0.0)
        status_text   = st.empty()
        already_known = len(all_eids) - missing_ct

        def _update_progress(done: int, total: int, found: int) -> None:
            frac = done / total if total else 1.0
            progress_bar.progress(min(frac, 1.0))
            status_text.caption(
                f"Fetching clips… {already_known + found}/{len(all_eids)} resolved"
            )

        with st.spinner(f"Fetching {missing_ct} video clip(s)…"):
            fetch_missing_videos(game_id, all_eids, state, _update_progress)

        progress_bar.empty()
        status_text.empty()
        game_store = video_store.get(game_id, {})

    hit  = sum(1 for eid in all_eids if game_store.get(eid))
    miss = len(all_eids) - hit

    col_status, col_retry = st.columns([5, 1])
    with col_status:
        st.caption(_fmt_resolve_status(hit, len(all_eids)))
    with col_retry:
        if miss > 0 and st.button("🔄 Retry", help="Re-attempt unresolved clips"):
            st.rerun()

    # ── Playlist (sidebar) ────────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.header("📺 Playlist")

    if "video_index" not in st.session_state:
        st.session_state.video_index = 0
    # Clamp index in case filter reduced the list
    st.session_state.video_index = min(
        st.session_state.video_index, len(filtered) - 1
    )

    idx        = st.session_state.video_index
    active     = filtered[idx]
    active_url = game_store.get(active["event_id"])

    st.sidebar.caption(f"Clip {idx + 1} / {len(filtered)}")
    st.sidebar.write(
        f"**{active['player']}** ({active['team']}) — {active['type']}"
    )
    st.sidebar.write(f"Q{active['quarter']} · {active['clock']}")

    if active_url:
        st.sidebar.video(active_url)
    else:
        st.sidebar.warning("Clip unavailable.")
        st.sidebar.markdown(f"[Open on NBA.com ↗]({fallback_link(game_id, active)})")

    col_prev, col_next = st.sidebar.columns(2)
    with col_prev:
        if st.button("⬅ Prev", key="prev"):
            st.session_state.video_index = (idx - 1) % len(filtered)
            st.rerun()
    with col_next:
        if st.button("Next ➡", key="next"):
            st.session_state.video_index = (idx + 1) % len(filtered)
            st.rerun()

    # ── Clip index (main panel) ───────────────────────────────────────────────
    st.markdown("### Clip index")

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
                    st.warning("Clip unavailable from NBA server.")
                    st.markdown(
                        f"[Open on NBA.com ↗]({fallback_link(game_id, entry)})"
                    )
        st.markdown("---")


if __name__ == "__main__":
    main()
