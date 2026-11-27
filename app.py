"""YouTube Niche Discovery Dashboard

Discovers recently-created YouTube channels through recent high-view videos.
Requires a YouTube Data API v3 key. Search API quota is limited; see README.md.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
import streamlit as st

APP_TITLE = "YouTube Niche Discovery"
DB_PATH = os.environ.get("YOUTUBE_DISCOVERY_DB", "youtube_discovery.db")
API_BASE = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT = 25

# These are deliberately broad seed queries, not a claim that every niche is covered.
DEFAULT_QUERIES = [
    "history documentary", "ancient history", "military history", "historical mysteries",
    "true story documentary", "survival stories", "emotional storytelling", "short stories",
    "crime documentary", "mystery documentary", "unsolved mysteries", "true events",
    "science documentary", "space documentary", "technology explained", "artificial intelligence",
    "business documentary", "personal finance", "economics explained", "biography documentary",
    "wildlife documentary", "nature documentary", "animal stories", "engineering inventions",
    "geography documentary", "architecture documentary", "psychology explained", "philosophy explained",
    "automotive documentary", "aviation history", "ocean documentary", "travel documentary",
    "food history", "mythology explained", "medieval history", "future technology",
]

EXCLUDED_COUNTRIES = {"PK", "IN", "BD", "LK"}
# Language tags are not perfectly standardized; matching uses the primary BCP-47 subtag.
EXCLUDED_LANGUAGE_TAGS = {
    "ur", "hi", "bn", "si", "ta", "te", "ml", "kn", "mr", "gu", "pa", "or",
    "as", "ne", "sd", "ks", "kok", "doi", "mai", "sa", "bo",
}

st.set_page_config(page_title=APP_TITLE, page_icon="🔎", layout="wide", initial_sidebar_state="expanded")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_youtube_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def channel_age_days(published_at: Optional[str]) -> Optional[int]:
    created = parse_youtube_datetime(published_at)
    if not created:
        return None
    return max(0, (utc_now() - created).days)


def primary_language_tag(tag: Optional[str]) -> Optional[str]:
    if not tag or not isinstance(tag, str):
        return None
    return tag.strip().replace("_", "-").split("-")[0].lower() or None


def language_is_excluded(tags: Iterable[Optional[str]]) -> bool:
    return any(primary_language_tag(tag) in EXCLUDED_LANGUAGE_TAGS for tag in tags if tag)


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT,
            age_days INTEGER,
            country TEXT,
            country_status TEXT,
            subscribers INTEGER,
            total_views INTEGER,
            video_count INTEGER,
            best_video_title TEXT,
            best_video_url TEXT,
            best_video_views INTEGER,
            best_video_language TEXT,
            category_query TEXT,
            discovered_at TEXT NOT NULL,
            raw_json TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            title TEXT,
            published_at TEXT,
            views INTEGER,
            language TEXT,
            audio_language TEXT,
            url TEXT,
            category_query TEXT,
            discovered_at TEXT NOT NULL
        )""")
        conn.commit()


def db_save_channel(row: Dict[str, Any]) -> None:
    fields = [
        "channel_id", "title", "url", "created_at", "age_days", "country", "country_status",
        "subscribers", "total_views", "video_count", "best_video_title", "best_video_url",
        "best_video_views", "best_video_language", "category_query", "discovered_at", "raw_json"
    ]
    values = [row.get(field) for field in fields]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"INSERT INTO channels ({','.join(fields)}) VALUES ({','.join(['?'] * len(fields))}) "
            "ON CONFLICT(channel_id) DO UPDATE SET " + ",".join(f"{f}=excluded.{f}" for f in fields[1:]),
            values,
        )
        conn.commit()


def db_save_video(row: Dict[str, Any]) -> None:
    fields = ["video_id", "channel_id", "title", "published_at", "views", "language", "audio_language", "url", "category_query", "discovered_at"]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"INSERT INTO videos ({','.join(fields)}) VALUES ({','.join(['?'] * len(fields))}) "
            "ON CONFLICT(video_id) DO UPDATE SET " + ",".join(f"{f}=excluded.{f}" for f in fields[1:]),
            [row.get(field) for field in fields],
        )
        conn.commit()


def db_load_channels() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY best_video_views DESC")]


def db_load_videos() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM videos ORDER BY views DESC")]


def db_clear() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM channels")
        conn.execute("DELETE FROM videos")
        conn.commit()


class YouTubeAPIError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.session = requests.Session()
        self.calls = 0

    def get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        query = dict(params)
        query["key"] = self.api_key
        try:
            response = self.session.get(f"{API_BASE}/{endpoint}", params=query, timeout=REQUEST_TIMEOUT)
            self.calls += 1
        except requests.RequestException as exc:
            raise YouTubeAPIError(f"Network error: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise YouTubeAPIError(f"YouTube returned a non-JSON response (HTTP {response.status_code}).") from exc
        if response.status_code >= 400:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = error.get("message", f"HTTP {response.status_code}")
            reasons = []
            for item in error.get("errors", []) if isinstance(error, dict) else []:
                if item.get("reason"):
                    reasons.append(item["reason"])
            suffix = f" (reason: {', '.join(reasons)})" if reasons else ""
            raise YouTubeAPIError(message + suffix)
        if not isinstance(payload, dict):
            raise YouTubeAPIError("Unexpected API response format.")
        return payload

    def search_videos(self, query: str, published_after: str, max_pages: int, min_video_views: int) -> List[Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        page_token: Optional[str] = None
        for _ in range(max_pages):
            params: Dict[str, Any] = {
                "part": "snippet", "type": "video", "q": query,
                "publishedAfter": published_after, "order": "viewCount",
                "maxResults": 50, "safeSearch": "none",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self.get("search", params)
            ids = [item.get("id", {}).get("videoId") for item in data.get("items", [])]
            ids = [item for item in ids if item]
            if ids:
                for video in self.get_videos(ids):
                    stats = video.get("statistics", {})
                    views = int(stats.get("viewCount", 0) or 0)
                    if views >= min_video_views:
                        found[video["id"]] = video
            page_token = data.get("nextPageToken")
            if not page_token:
                break
            # Be gentle with API and avoid accidental request bursts.
            time.sleep(0.08)
        return list(found.values())

    def get_videos(self, ids: Sequence[str]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            data = self.get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk), "maxResults": 50})
            output.extend(data.get("items", []))
        return output

    def get_channels(self, ids: Sequence[str]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        unique_ids = list(dict.fromkeys(ids))
        for start in range(0, len(unique_ids), 50):
            chunk = unique_ids[start:start + 50]
            data = self.get("channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk), "maxResults": 50})
            output.extend(data.get("items", []))
        return output


def video_language_tags(video: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    snippet = video.get("snippet", {})
    return snippet.get("defaultLanguage"), snippet.get("defaultAudioLanguage")


def make_video_row(video: Dict[str, Any], query: str) -> Dict[str, Any]:
    snippet = video.get("snippet", {})
    stats = video.get("statistics", {})
    default_lang, audio_lang = video_language_tags(video)
    vid = video.get("id", "")
    return {
        "video_id": vid,
        "channel_id": snippet.get("channelId", ""),
        "title": snippet.get("title", "Untitled"),
        "published_at": snippet.get("publishedAt"),
        "views": int(stats.get("viewCount", 0) or 0),
        "language": default_lang,
        "audio_language": audio_lang,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "category_query": query,
        "discovered_at": utc_now().isoformat(),
    }


def channel_row(channel: Dict[str, Any], channel_videos: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    snippet = channel.get("snippet", {})
    stats = channel.get("statistics", {})
    created_at = snippet.get("publishedAt")
    age = channel_age_days(created_at)
    if age is None or not (7 <= age <= 90):
        return None
    country = snippet.get("country")
    country_status = "Known" if country else "Unknown"
    # Country exclusion applies only when YouTube provides a country value.
    if country and country.upper() in EXCLUDED_COUNTRIES:
        return None

    eligible_videos = []
    for video in channel_videos:
        v_snippet = video.get("snippet", {})
        v_stats = video.get("statistics", {})
        views = int(v_stats.get("viewCount", 0) or 0)
        if views < 20000:
            continue
        default_lang, audio_lang = video_language_tags(video)
        if language_is_excluded((default_lang, audio_lang)):
            continue
        eligible_videos.append((views, video, default_lang or audio_lang))
    if not eligible_videos:
        return None
    eligible_videos.sort(key=lambda x: x[0], reverse=True)
    best_views, best_video, best_language = eligible_videos[0]
    best_id = best_video.get("id", "")
    return {
        "channel_id": channel.get("id", ""),
        "title": snippet.get("title", "Untitled channel"),
        "url": f"https://www.youtube.com/channel/{channel.get('id', '')}",
        "created_at": created_at,
        "age_days": age,
        "country": country,
        "country_status": country_status,
        "subscribers": int(stats.get("subscriberCount", 0) or 0) if "subscriberCount" in stats else None,
        "total_views": int(stats.get("viewCount", 0) or 0),
        "video_count": int(stats.get("videoCount", 0) or 0),
        "best_video_title": best_video.get("snippet", {}).get("title", "Untitled video"),
        "best_video_url": f"https://www.youtube.com/watch?v={best_id}",
        "best_video_views": best_views,
        "best_video_language": best_language,
        "category_query": query,
        "discovered_at": utc_now().isoformat(),
        "raw_json": json.dumps(channel, ensure_ascii=False),
    }


def discover(client: YouTubeClient, queries: Sequence[str], max_pages: int, min_views: int,
             age_days_min: int, age_days_max: int, progress, status) -> Tuple[int, int, List[str]]:
    # Search is limited to videos published within the requested maximum channel-age window.
    published_after = (utc_now() - timedelta(days=age_days_max)).isoformat(timespec="seconds").replace("+00:00", "Z")
    videos_by_id: Dict[str, Tuple[Dict[str, Any], str]] = {}
    errors: List[str] = []
    for index, query in enumerate(queries, start=1):
        status.write(f"Searching {index}/{len(queries)}: **{query}**")
        try:
            results = client.search_videos(query, published_after, max_pages, min_views)
            for video in results:
                videos_by_id[video["id"]] = (video, query)
        except YouTubeAPIError as exc:
            errors.append(f"Search '{query}': {exc}")
            if "quota" in str(exc).lower() or "dailyLimitExceeded" in str(exc):
                errors.append("YouTube API quota appears exhausted. Discovery stopped to avoid repeated failed requests.")
                break
        progress.progress(index / max(1, len(queries)), text=f"Queries completed: {index}/{len(queries)}")

    if not videos_by_id:
        return 0, client.calls, errors

    video_ids = list(videos_by_id.keys())
    # Search response already identifies channel IDs; fetch full video details for language/statistics.
    try:
        full_videos = client.get_videos(video_ids)
    except YouTubeAPIError as exc:
        errors.append(f"Video detail lookup: {exc}")
        return 0, client.calls, errors

    channel_to_videos: Dict[str, List[Dict[str, Any]]] = {}
    channel_query: Dict[str, str] = {}
    for video in full_videos:
        channel_id = video.get("snippet", {}).get("channelId")
        if not channel_id:
            continue
        channel_to_videos.setdefault(channel_id, []).append(video)
        channel_query.setdefault(channel_id, videos_by_id.get(video.get("id"), ({}, "Unknown"))[1])

    try:
        channels = client.get_channels(list(channel_to_videos.keys()))
    except YouTubeAPIError as exc:
        errors.append(f"Channel detail lookup: {exc}")
        return 0, client.calls, errors

    saved = 0
    for channel in channels:
        cid = channel.get("id")
        if not cid:
            continue
        row = channel_row(channel, channel_to_videos.get(cid, []), channel_query.get(cid, "Unknown"))
        if row and age_days_min <= row["age_days"] <= age_days_max:
            db_save_channel(row)
            saved += 1
            # Persist only videos from channels that passed the channel-level country/age/language checks.
            for accepted_video in channel_to_videos.get(cid, []):
                accepted_views = int(accepted_video.get("statistics", {}).get("viewCount", 0) or 0)
                lang, audio_lang = video_language_tags(accepted_video)
                if accepted_views >= min_views and not language_is_excluded((lang, audio_lang)):
                    db_save_video(make_video_row(accepted_video, channel_query.get(cid, "Unknown")))
    return saved, client.calls, errors


def export_csv(rows: List[Dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def apply_display_filters(rows: List[Dict[str, Any]], min_views: int, min_age: int, max_age: int,
                           include_unknown_country: bool, allowed_countries: List[str]) -> List[Dict[str, Any]]:
    result = []
    for row in rows:
        if int(row.get("best_video_views") or 0) < min_views:
            continue
        age = row.get("age_days")
        if age is None or not min_age <= int(age) <= max_age:
            continue
        country = row.get("country")
        if not country and not include_unknown_country:
            continue
        if country and allowed_countries and country.upper() not in allowed_countries:
            continue
        if country and country.upper() in EXCLUDED_COUNTRIES:
            continue
        result.append(row)
    return sorted(result, key=lambda r: int(r.get("best_video_views") or 0), reverse=True)


init_db()

st.markdown("""
<style>
    .stApp {background: #0b1020; color: #e8edf7;}
    [data-testid="stSidebar"] {background: #111a2e;}
    div[data-testid="stMetric"] {background: #121d33; border: 1px solid #263653; padding: 14px; border-radius: 12px;}
    div[data-testid="stDataFrame"] {border: 1px solid #263653; border-radius: 10px;}
    .hero {padding: 18px 22px; border: 1px solid #263653; border-radius: 14px; background: linear-gradient(110deg,#14213b,#10182b); margin-bottom: 18px;}
    .muted {color:#9aa9c2; font-size:0.9rem;}
</style>
""", unsafe_allow_html=True)

st.markdown(f'<div class="hero"><h1 style="margin:0">🔎 {APP_TITLE}</h1><p class="muted">Discover emerging YouTube channels · Analyze high-view videos · Save research locally</p></div>', unsafe_allow_html=True)

with st.sidebar:
    st.header("Research settings")
    api_key = st.text_input("YouTube Data API v3 key", type="password", value=os.environ.get("YOUTUBE_API_KEY", ""), help="Create an API key in Google Cloud Console and enable YouTube Data API v3.")
    st.caption("Your API key is used only by this local app and is not saved in the database.")
    st.divider()
    st.subheader("Discovery filters")
    age_min = st.number_input("Minimum channel age (days)", min_value=1, max_value=365, value=7)
    age_max = st.number_input("Maximum channel age (days)", min_value=1, max_value=365, value=90)
    min_views = st.number_input("Minimum views on one video", min_value=1, value=20000, step=5000)
    pages_per_query = st.select_slider("Search pages per query", options=[1, 2, 3, 4, 5], value=1, help="Each page is a YouTube search request and consumes API quota.")
    st.divider()
    st.subheader("Country filter")
    include_unknown_country = st.checkbox("Include channels with unknown country", value=False, help="If off, channels without a public country value are hidden from the results view. They remain in the database only if they passed other checks.")
    country_text = st.text_input("Optional allowed country codes (comma-separated)", value="", placeholder="US,GB,CA,AU,NZ")
    allowed_countries = [x.strip().upper() for x in country_text.split(",") if x.strip()]
    st.caption("Always excluded: PK, IN, BD, LK. Allowed-country list is optional.")

    st.divider()
    st.subheader("Search queries")
    query_text = st.text_area("One topic / niche per line", value="\n".join(DEFAULT_QUERIES), height=220)
    st.caption("Search across many queries to broaden discovery. YouTube search is not exhaustive.")

if age_min > age_max:
    st.error("Minimum channel age cannot be greater than maximum channel age.")
    st.stop()

queries = list(dict.fromkeys(line.strip() for line in query_text.splitlines() if line.strip()))

col_a, col_b, col_c = st.columns([1.2, 1, 1])
with col_a:
    st.markdown("**Discovery criteria**")
    st.write(f"Channel age: **{age_min}–{age_max} days** · At least one video: **{format_number(min_views)}+ views**")
with col_b:
    st.markdown("**Queries loaded**")
    st.metric("Niche search terms", len(queries))
with col_c:
    st.markdown("**Excluded countries**")
    st.metric("Country codes", "PK · IN · BD · LK")

run_col, refresh_col, clear_col = st.columns([1.3, 1, 1])
with run_col:
    run_search = st.button("▶ Start channel discovery", type="primary", use_container_width=True)
with refresh_col:
    refresh_view = st.button("↻ Refresh saved results", use_container_width=True)
with clear_col:
    clear_saved = st.button("Delete saved research", use_container_width=True)

if clear_saved:
    db_clear()
    st.success("Saved research database cleared.")
    st.rerun()

if run_search:
    if not api_key.strip():
        st.error("API key missing. Add your YouTube Data API v3 key in the sidebar.")
    elif not queries:
        st.error("Add at least one search query.")
    else:
        client = YouTubeClient(api_key)
        progress = st.progress(0, text="Preparing discovery...")
        status = st.empty()
        with st.spinner("Searching YouTube and checking channel/video details..."):
            saved, api_calls, errors = discover(client, queries, int(pages_per_query), int(min_views), int(age_min), int(age_max), progress, status)
        progress.empty()
        status.empty()
        st.success(f"Discovery finished. {saved} qualifying channel record(s) saved. API requests made: {api_calls}.")
        if errors:
            with st.expander(f"Warnings / API errors ({len(errors)})", expanded=True):
                for error in errors:
                    st.warning(error)

all_channels = db_load_channels()
visible = apply_display_filters(all_channels, int(min_views), int(age_min), int(age_max), include_unknown_country, allowed_countries)

st.divider()
st.header("Discovered channels")
metric1, metric2, metric3, metric4 = st.columns(4)
metric1.metric("Saved qualifying channels", format_number(len(all_channels)))
metric2.metric("Visible channels", format_number(len(visible)))
metric3.metric("Highest video views", format_number(max((int(r.get("best_video_views") or 0) for r in visible), default=0)))
metric4.metric("Average channel age", f"{sum(int(r.get('age_days') or 0) for r in visible) / len(visible):.1f} days" if visible else "—")

if visible:
    search_filter = st.text_input("Filter results by channel name or niche", placeholder="Type a channel name or search query...")
    if search_filter:
        needle = search_filter.casefold()
        visible = [r for r in visible if needle in str(r.get("title", "")).casefold() or needle in str(r.get("category_query", "")).casefold()]

    display_rows = []
    for r in visible:
        display_rows.append({
            "Channel": r.get("title"),
            "Channel age (days)": r.get("age_days"),
            "Country": r.get("country") or "Unknown",
            "Subscribers": format_number(r.get("subscribers")) if r.get("subscribers") is not None else "Hidden",
            "Channel views": r.get("total_views"),
            "Videos": r.get("video_count"),
            "Best video views": r.get("best_video_views"),
            "Video language": r.get("best_video_language") or "Unknown",
            "Discovery topic": r.get("category_query"),
            "Channel URL": r.get("url"),
            "Best video URL": r.get("best_video_url"),
        })
    st.dataframe(display_rows, use_container_width=True, hide_index=True, height=460,
                 column_config={
                     "Channel URL": st.column_config.LinkColumn("Channel URL", display_text="Open channel"),
                     "Best video URL": st.column_config.LinkColumn("Best video URL", display_text="Watch video"),
                     "Best video views": st.column_config.NumberColumn(format="%d"),
                     "Channel views": st.column_config.NumberColumn(format="%d"),
                 })
    st.download_button("⬇ Download visible channels (CSV)", data=export_csv(visible), file_name="youtube_discovered_channels.csv", mime="text/csv")

    st.subheader("Top opportunities")
    top = visible[:10]
    for index, row in enumerate(top, start=1):
        with st.expander(f"{index}. {row.get('title')} — {format_number(row.get('best_video_views'))} views"):
            st.write(f"**Channel age:** {row.get('age_days')} days")
            st.write(f"**Country:** {row.get('country') or 'Unknown'}")
            st.write(f"**Best video:** {row.get('best_video_title')}")
            st.write(f"**Search topic:** {row.get('category_query')}")
            st.markdown(f"[Open channel]({row.get('url')}) · [Watch best video]({row.get('best_video_url')})")
else:
    st.info("No matching saved channels yet. Add an API key and start discovery. If you already searched, try more search queries or include unknown-country channels to inspect unclassified results.")

with st.expander("Saved high-view videos"):
    saved_videos = db_load_videos()
    if saved_videos:
        video_display = [{
            "Video": v.get("title"), "Views": v.get("views"), "Published": v.get("published_at"),
            "Language": v.get("language") or v.get("audio_language") or "Unknown",
            "Search topic": v.get("category_query"), "Video URL": v.get("url")
        } for v in saved_videos if int(v.get("views") or 0) >= int(min_views)]
        st.dataframe(video_display, use_container_width=True, hide_index=True, height=300,
                     column_config={"Video URL": st.column_config.LinkColumn("Video URL", display_text="Watch"), "Views": st.column_config.NumberColumn(format="%d")})
        st.download_button("Download video research (CSV)", data=export_csv(saved_videos), file_name="youtube_video_research.csv", mime="text/csv")
    else:
        st.caption("No video records saved yet.")

with st.expander("How the discovery logic works / limitations"):
    st.markdown("""
    - The app searches videos published within the maximum channel-age window, then checks each channel's actual creation date.
    - A channel qualifies only when it is within the selected age range and has at least one recent video with the minimum view count.
    - The API's `search.list` searches videos, not every channel on YouTube. Results depend on the search phrases and YouTube's ranking; this is not an exhaustive census.
    - Country is read from the channel's public `snippet.country` field. If it is absent, country is Unknown.
    - Language is read from video `defaultLanguage` / `defaultAudioLanguage` when available. Many videos do not expose these fields. Language is therefore not a reliable channel-wide classification.
    - The excluded-language list is a practical code-based filter, not a perfect detector of spoken language. Review unknown-language results manually.
    - YouTube Data API quota is finite. Search requests are quota-expensive; increase pages/queries gradually and monitor Google Cloud Console.
    - Subscriber counts may be hidden or rounded by YouTube. The app does not estimate hidden subscriber counts.
    """)

st.caption(f"Local database: {DB_PATH} · Last app refresh: {utc_now().strftime('%Y-%m-%d %H:%M UTC')}")
