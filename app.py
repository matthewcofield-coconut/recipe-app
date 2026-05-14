import os
import base64
import io
import json
import smtplib
import subprocess
import sys
import anthropic
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from ytmusicapi import YTMusic
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import feedparser
import functools
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, Response

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-dev-key")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "changeme")

GMAIL_USER = "Matthew.cofield@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
BRIEFING_SECRET = os.environ.get("BRIEFING_SECRET")

CENTRAL = ZoneInfo("America/Chicago")

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != AUTH_USERNAME or auth.password != AUTH_PASSWORD:
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="Recipe App"'}
            )
        return f(*args, **kwargs)
    return decorated

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "https://recipe-app-q87n.onrender.com/callback"
SPOTIFY_SCOPE = "playlist-modify-public playlist-modify-private user-read-private user-read-email"

RECIPE_FORMAT = """RECIPE NAME: <name>

INGREDIENTS:
- <ingredient 1>
- <ingredient 2>
...

INSTRUCTIONS:
1. <step 1>
2. <step 2>
...

CALORIES: <total calories per serving, only if explicitly stated — otherwise omit this line entirely>"""


# ── Recipe helpers ────────────────────────────────────────────────────────────

def search_urls(query: str, n: int = 6) -> list[str]:
    try:
        with DDGS() as ddgs:
            return [r["href"] for r in ddgs.text(query, max_results=n)]
    except Exception:
        return []


def build_queries(ingredients: list[str]) -> list[str]:
    all_ing = ", ".join(ingredients)
    primary = ingredients[0] if ingredients else ""
    return [
        f"best recipe with {all_ing} allrecipes OR foodnetwork OR seriouseats OR budgetbytes",
        f"easy recipe {all_ing}",
        f"recipe using {primary}",
        f"simple {primary} recipe",
    ]


def scrape_page(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=8)
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:7000]
    except Exception:
        return ""


def extract_from_page(raw_text: str, ingredients: list[str]) -> str:
    ing_str = ", ".join(ingredients)
    prompt = f"""You are a recipe extractor. The user has these ingredients: {ing_str}.

From the text below, extract ONE complete recipe that best uses those ingredients. If multiple recipes appear, pick the one most relevant to the user's ingredients.

Return the recipe in exactly this format — nothing else:

{RECIPE_FORMAT}

Rules:
- No links, source credits, ads, commentary, or fluff
- If the text does not contain a clear recipe with ingredients AND instructions, reply only with: NO_RECIPE

Text:
{raw_text}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def generate_from_knowledge(ingredients: list[str], exclude: list[str] = []) -> str:
    exclude_str = f"\nDo NOT suggest any of these recipes: {', '.join(exclude)}." if exclude else ""
    prompt = f"""The user has these ingredients: {', '.join(ingredients)}.

Suggest the best recipe you can make using most or all of these ingredients. You may include a few common pantry staples (salt, pepper, oil, butter, garlic, onion) even if not listed.{exclude_str}

Return the recipe in exactly this format — nothing else:

{RECIPE_FORMAT}

No links, no commentary, no fluff. Just the recipe."""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Briefing helpers ──────────────────────────────────────────────────────────

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


def get_weather() -> str:
    try:
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": 34.1015, "longitude": -84.5194,
            "current": "temperature_2m,weather_code,precipitation_probability",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
            "forecast_days": 7,
        }, timeout=10)
        data = resp.json()
        current = data["current"]
        daily = data["daily"]

        now_temp = round(current["temperature_2m"])
        now_desc = WMO_CODES.get(current["weather_code"], "Unknown")
        now_precip = current["precipitation_probability"]
        today_line = f"Right now: {now_desc}, {now_temp}°F, {now_precip}% chance of rain"

        lines = [today_line, ""]
        for i in range(7):
            dt = datetime.strptime(daily["time"][i], "%Y-%m-%d")
            desc = WMO_CODES.get(daily["weather_code"][i], "Unknown")
            high = round(daily["temperature_2m_max"][i])
            low = round(daily["temperature_2m_min"][i])
            precip = daily["precipitation_probability_max"][i]
            lines.append(f"{dt.strftime('%A, %b %d')}: {desc}, {high}°F / {low}°F, {precip}% rain")
        return "\n".join(lines)
    except Exception as e:
        return f"Weather unavailable: {e}"



def get_auburn_news() -> dict:
    feeds = {
        "Basketball": "https://news.google.com/rss/search?q=Auburn+Tigers+basketball+when:1d&hl=en-US&gl=US&ceid=US:en",
        "Football": "https://news.google.com/rss/search?q=Auburn+Tigers+football+when:1d&hl=en-US&gl=US&ceid=US:en",
    }
    results = {}
    for sport, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            items = feed.entries[:4]
            if items:
                results[sport] = "\n".join(
                    f"• {e.get('title', 'No title')} ({e.get('source', {}).get('title', '')}) — {e.get('link', '')}"
                    for e in items
                )
            else:
                results[sport] = "No recent news."
        except Exception as e:
            results[sport] = f"Unavailable: {e}"
    return results


def generate_briefing_html(weather: str, news: dict) -> str:
    today = datetime.now(CENTRAL).strftime("%A, %B %d, %Y")
    prompt = f"""You are writing a daily morning briefing email for Matthew. Today is {today}.

DATA:

WEATHER — Woodstock, GA (current conditions + 7-day forecast):
{weather}

AUBURN BASKETBALL NEWS:
{news.get('Basketball', 'N/A')}

AUBURN FOOTBALL NEWS:
{news.get('Football', 'N/A')}

Write a clean, friendly morning briefing as an HTML email. Requirements:
- Warm greeting mentioning today's date
- Weather section: today's conditions up front, then the full 7-day outlook in a simple table or list
- Auburn sports section: basketball then football headlines, each as a clickable <a href="..."> link using the URL provided
- Tone: casual and personal, like a message from a helpful friend
- Use inline CSS only (no external stylesheets). Keep it clean and readable on mobile.
- Output only the HTML — no markdown fences, no explanation."""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    html = msg.content[0].text.strip()
    # Strip markdown code fences if Claude wraps the output
    if html.startswith("```"):
        html = html.split("\n", 1)[-1]
    if html.endswith("```"):
        html = html.rsplit("```", 1)[0]
    return html.strip()


def send_briefing_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_USER
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())


# ── Spotify helpers ───────────────────────────────────────────────────────────

def get_spotify():
    token_info = session.get("spotify_token")
    if not token_info:
        return None
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
    )
    if sp_oauth.is_token_expired(token_info):
        token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
        session["spotify_token"] = token_info
    return spotipy.Spotify(auth=token_info["access_token"])


YTMUSIC_HEADERS_FILE = os.environ.get("YTMUSIC_HEADERS_FILE", os.path.expanduser("~/browser.json"))


def get_ytmusic():
    if not os.path.exists(YTMUSIC_HEADERS_FILE):
        return None
    try:
        return YTMusic(YTMUSIC_HEADERS_FILE)
    except Exception:
        return None


def claude_pick_tracks(vibe: str) -> tuple[str, list[dict]]:
    prompt = f"""The user wants a music playlist with this vibe: "{vibe}"

First, produce a short playlist title (2–5 words, title case, evocative, no quotes, no emojis). It should accurately describe the resulting mix — not just echo the user's prompt verbatim. Avoid generic words like "Playlist", "Vibes", "Mix" unless they're essential.

Then return exactly 20 tracks that fit this vibe.

Important interpretation rules:
- When the user names an artist as a reference (e.g. "John Mayer-like", "songs like Phoebe Bridgers", "Bon Iver vibes"), they want songs SIMILAR IN STYLE to that artist — NOT songs by that artist. Pick tracks by other artists who share the relevant qualities (guitar tone, vocal style, mood, production, lyrical register, etc.).
- Only include songs by a referenced artist if the user explicitly says so ("include some John Mayer", "with John Mayer mixed in").
- Vary the artists across the 20 tracks — no more than 2 songs by the same artist unless the user asks for a single-artist mix.
- Treat genre/mood descriptors (e.g. "lofi beat", "moody", "summery") as hard constraints — every track should plausibly match the dominant vibe, not just the reference artist.

For each track return the exact song title and the exact artist name, separated by a pipe character.

Output format (exactly this, nothing else):
TITLE: <playlist title>
---
track title | artist name
track title | artist name
... (20 tracks total)

Be specific — use the correct artist so there is no ambiguity with other songs of the same name."""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    title = ""
    tracks = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line == "---":
            continue
        if line.upper().startswith("TITLE:") and not title:
            title = line.split(":", 1)[1].strip().strip('"').strip("'")
            continue
        if "|" in line:
            parts = line.split("|", 1)
            tracks.append({"title": parts[0].strip(), "artist": parts[1].strip()})
    if not title:
        title = vibe.title()
    return title, tracks


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/playlist")
@require_auth
def playlist_page():
    return render_template("playlist.html")


@app.route("/spotify/login")
def spotify_login():
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
    )
    return redirect(sp_oauth.get_authorize_url())


@app.route("/callback")
def spotify_callback():
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
    )
    code = request.args.get("code")
    token_info = sp_oauth.get_access_token(code)
    session["spotify_token"] = token_info
    return redirect(url_for("playlist_page"))


@app.route("/spotify/status")
def spotify_status():
    sp = get_spotify()
    if not sp:
        return jsonify({"connected": False})
    user = sp.current_user()
    return jsonify({"connected": True, "name": user["display_name"]})


@app.route("/spotify/create", methods=["POST"])
def spotify_create():
    sp = get_spotify()
    if not sp:
        return jsonify({"error": "Not connected to Spotify"}), 401

    data = request.get_json()
    vibe = data.get("vibe", "").strip()
    if not vibe:
        return jsonify({"error": "No vibe provided"}), 400

    tracks = claude_pick_tracks(vibe)
    track_uris = []
    for track in tracks:
        query = f'track:"{track["title"]}" artist:"{track["artist"]}"'
        results = sp.search(q=query, type="track", limit=1)
        items = results["tracks"]["items"]
        if items:
            track_uris.append(items[0]["uri"])

    if not track_uris:
        return jsonify({"error": "Couldn't find any tracks. Try a different vibe."}), 500

    playlist = sp._post("me/playlists", payload={
        "name": vibe.title(),
        "public": True,
        "description": f"Generated by Recipe Maker · {vibe}"
    })
    sp.playlist_add_items(playlist["id"], track_uris)

    return jsonify({
        "name": playlist["name"],
        "tracks": len(track_uris),
        "url": playlist["external_urls"]["spotify"],
    })


@app.route("/ytm/status")
def ytm_status():
    ytm = get_ytmusic()
    if not ytm:
        return jsonify({"connected": False, "reason": f"Missing {YTMUSIC_HEADERS_FILE}"})
    try:
        info = ytm.get_account_info()
        name = info.get("accountName") or "YouTube Music"
        return jsonify({"connected": True, "name": name})
    except Exception as e:
        return jsonify({
            "connected": False,
            "reason": f"Headers loaded but auth probe failed — cookies likely expired ({type(e).__name__})",
        })


@app.route("/ytm/refresh", methods=["POST"])
def ytm_refresh():
    script = os.path.expanduser("~/build_ytm_headers.py")
    if not os.path.exists(script):
        return jsonify({"ok": False, "error": f"Refresh script not found at {script}"}), 404
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Refresh script timed out (Keychain prompt?)"}), 504
    if result.returncode != 0:
        return jsonify({
            "ok": False,
            "error": "Refresh script failed",
            "stderr": result.stderr[-500:],
            "stdout": result.stdout[-500:],
        }), 500
    return jsonify({"ok": True, "output": result.stdout[-500:]})


@app.route("/ytm/create", methods=["POST"])
def ytm_create():
    ytm = get_ytmusic()
    if not ytm:
        return jsonify({"error": "YouTube Music not connected. Refresh browser.json."}), 401

    data = request.get_json() or {}
    vibe = (data.get("vibe") or "").strip()
    if not vibe:
        return jsonify({"error": "No vibe provided"}), 400

    title, tracks = claude_pick_tracks(vibe)
    video_ids = []
    for track in tracks:
        query = f'{track["title"]} {track["artist"]}'
        try:
            results = ytm.search(query, filter="songs", limit=1)
        except Exception:
            continue
        if results and results[0].get("videoId"):
            video_ids.append(results[0]["videoId"])

    if not video_ids:
        return jsonify({"error": "Couldn't find any tracks. Try a different vibe."}), 500

    try:
        result = ytm.create_playlist(
            title=title,
            description=f"Generated by Recipe Maker · {vibe}",
            privacy_status="PRIVATE",
            video_ids=video_ids,
        )
    except Exception as e:
        return jsonify({"error": f"YT Music rejected playlist create: {e}"}), 500

    if isinstance(result, str):
        playlist_id = result
    else:
        return jsonify({"error": "YT Music silently rate-limited. Try again later."}), 503

    return jsonify({
        "name": title,
        "tracks": len(video_ids),
        "url": f"https://music.youtube.com/playlist?list={playlist_id}",
    })


@app.route("/spotify/debug")
def spotify_debug():
    sp = get_spotify()
    if not sp:
        return jsonify({"error": "Not logged in"})
    user = sp.current_user()
    token = session.get("spotify_token", {})
    return jsonify({
        "user_id": user["id"],
        "email": user.get("email"),
        "product": user.get("product"),
        "scope": token.get("scope"),
    })



@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    ingredients = [i.strip() for i in data.get("ingredients", "").split(",") if i.strip()]
    if not ingredients:
        return jsonify({"error": "No ingredients provided"}), 400

    recipes = []
    seen_names = set()
    scrape_attempts = 0
    max_scrape_attempts = 3

    for query in build_queries(ingredients):
        if len(recipes) >= 3 or scrape_attempts >= max_scrape_attempts:
            break
        for url in search_urls(query):
            if len(recipes) >= 3 or scrape_attempts >= max_scrape_attempts:
                break
            raw = scrape_page(url)
            if not raw:
                continue
            scrape_attempts += 1
            result = extract_from_page(raw, ingredients)
            if not result or result == "NO_RECIPE":
                continue
            name_line = next((l for l in result.splitlines() if l.startswith("RECIPE NAME:")), "")
            name = name_line.replace("RECIPE NAME:", "").strip().lower()
            if name and name in seen_names:
                continue
            seen_names.add(name)
            recipes.append(result)

    while len(recipes) < 3:
        fallback = generate_from_knowledge(ingredients, exclude=list(seen_names))
        if not fallback:
            break
        name_line = next((l for l in fallback.splitlines() if l.startswith("RECIPE NAME:")), "")
        name = name_line.replace("RECIPE NAME:", "").strip().lower()
        if name in seen_names:
            break
        seen_names.add(name)
        recipes.append(fallback)

    if not recipes:
        return jsonify({"error": "Could not find a recipe. Try different ingredients."}), 500

    return jsonify({"recipes": recipes})


@app.route("/translator")
@require_auth
def translator_page():
    return render_template("translator.html")


@app.route("/translator/translate", methods=["POST"])
@require_auth
def translate():
    language = request.form.get("language", "Spanish (Mexican)")
    text_input = request.form.get("text_input", "").strip()
    file = request.files.get("file")

    LANGUAGE_INSTRUCTIONS = {
        "Spanish (Mexican)": (
            "Translate the following into Mexican Spanish (Latin American Spanish as used in Mexico). "
            "Use 'ustedes' instead of 'vosotros'. Infer the tone from the content — "
            "casual/fun content should feel natural and warm, not stiff or overly formal. "
            "Use conjugations and phrasing that feel native, not textbook."
        ),
        "Swahili": (
            "Translate the following into standard Swahili as spoken in Tanzania/Kenya. "
            "Infer the tone from the content — casual/fun content should feel natural and engaging, "
            "not overly formal. Use phrasing that feels natural to a native speaker."
        ),
    }

    lang_instruction = LANGUAGE_INSTRUCTIONS.get(language, LANGUAGE_INSTRUCTIONS["Spanish (Mexican)"])

    prompt = (
        f"{lang_instruction}\n\n"
        "Return your response in exactly this format — nothing else:\n\n"
        "TRANSLATION:\n[full translated text]\n\n"
        "REASONING:\n[2-4 sentences explaining your tone decisions and any notable translation choices]"
    )

    content = []

    if file and file.filename:
        filename = file.filename.lower()
        file_data = file.read()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""

        if ext in ("png", "jpg", "jpeg", "gif", "webp"):
            media_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "gif": "image/gif", "webp": "image/webp"}
            content = [
                {"type": "image", "source": {"type": "base64",
                 "media_type": media_map[ext], "data": base64.standard_b64encode(file_data).decode()}},
                {"type": "text", "text": prompt},
            ]
        elif ext == "pdf":
            content = [
                {"type": "document", "source": {"type": "base64",
                 "media_type": "application/pdf", "data": base64.standard_b64encode(file_data).decode()}},
                {"type": "text", "text": prompt},
            ]
        elif ext == "docx":
            from docx import Document
            doc = Document(io.BytesIO(file_data))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            content = [{"type": "text", "text": f"Document text:\n\n{text}\n\n{prompt}"}]
        elif ext == "txt":
            text = file_data.decode("utf-8", errors="replace")
            content = [{"type": "text", "text": f"Document text:\n\n{text}\n\n{prompt}"}]
        else:
            return jsonify({"error": "Unsupported file type. Please upload an image, PDF, DOCX, or TXT file."}), 400

    elif text_input:
        content = [{"type": "text", "text": f"Text to translate:\n\n{text_input}\n\n{prompt}"}]
    else:
        return jsonify({"error": "Please paste some text or upload a file."}), 400

    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )

    response = msg.content[0].text.strip()
    translation, reasoning = "", ""

    if "TRANSLATION:" in response and "REASONING:" in response:
        parts = response.split("REASONING:", 1)
        translation = parts[0].replace("TRANSLATION:", "").strip()
        reasoning = parts[1].strip()
    else:
        translation = response

    return jsonify({"translation": translation, "reasoning": reasoning})


# ── Car research helpers ──────────────────────────────────────────────────────

CAR_PICK_FORMAT = """For each of the 3 recommendations, output exactly this block — separated by a line containing only ---

MODEL: <year> <make> <model> <trim if relevant>
PRICE: <typical out-the-door price range, e.g. "$28k–$34k">
WHY: <1–2 sentences on why it fits the user's criteria>
SPECS:
- MPG: <city/hwy or combined>
- Seats: <number>
- Cargo: <cu ft or short note>
- Drivetrain: <FWD/AWD/RWD, engine note if relevant>
PROS:
- <pro 1>
- <pro 2>
- <pro 3>
CONS:
- <con 1>
- <con 2>
WATCH FOR: <1 sentence on common reliability issues or things to inspect>"""


CAR_COMPARE_FORMAT = """Output a head-to-head comparison in exactly this format:

VERDICT: <1–2 sentence summary naming the winner overall and for whom each car is better>

CATEGORIES:
- Price & value: <comparison>
- Reliability: <comparison>
- Performance & driving: <comparison>
- Interior & comfort: <comparison>
- Tech & safety: <comparison>
- Fuel economy: <comparison>
- Cargo & practicality: <comparison>
- Resale value: <comparison>

BEST FOR:
- <car A name>: <type of buyer who should pick this>
- <car B name>: <type of buyer who should pick this>"""


CAR_DEEPDIVE_FORMAT = """Output exactly this format:

OVERVIEW: <2–3 sentence summary of the car and its reputation>

PRICING:
- New MSRP range: <range>
- Typical out-the-door: <range>
- Used (2–4 yr old): <range>

KEY SPECS:
- MPG: <city/hwy>
- Horsepower: <hp>
- Seats: <#>
- Cargo: <cu ft>
- Drivetrain options: <list>

PROS:
- <pro 1>
- <pro 2>
- <pro 3>
- <pro 4>

CONS:
- <con 1>
- <con 2>
- <con 3>

COMMON PROBLEMS:
- <known issue 1 — what years, what to inspect>
- <known issue 2>
- <known issue 3>

ALTERNATIVES: <2–3 competing models the buyer should also consider, with a phrase on each>

DEALER TIPS:
- <negotiation tip 1>
- <negotiation tip 2>
- <negotiation tip 3>
- <what to ask / inspect on a test drive>"""


def claude_pick_cars(criteria: str, body_style: str = "") -> str:
    style_line = f"\nBody style preference: {body_style}." if body_style and body_style.lower() != "any" else ""
    prompt = f"""You are a knowledgeable, no-nonsense car-buying advisor. The user is shopping for a new (or near-new) car and described what they want:

\"\"\"{criteria}\"\"\"{style_line}

Recommend exactly 3 specific models that best fit. Favor models known for reliability and good resale unless the user explicitly prioritizes something else. Mix at least one value pick and one slightly aspirational pick if the budget allows.

Return your answer in exactly this format — nothing else, no preamble, no closing remarks:

{CAR_PICK_FORMAT}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


CAR_TCO_FORMAT = """Output exactly this format:

SUMMARY: <1–2 sentences on overall ownership cost positioning>

YEARLY:
- Fuel: <$/year, with assumption like 12,000 mi/yr at $3.50/gal>
- Insurance: <$/year, ballpark for a typical 35yo with clean record>
- Maintenance & repairs: <$/year average over 5 years>
- Depreciation: <$/year, the biggest line item for new cars>

5-YEAR TOTAL: <total $ across all categories, plus monthly equivalent>

NOTES:
- <key caveat, e.g. EV charging vs. gas, premium fuel, brand-specific maintenance>
- <regional variation note>"""


CAR_CHECKLIST_FORMAT = """Output exactly this format:

EXTERIOR:
- <inspection item 1>
- <inspection item 2>
- <inspection item 3>
- <inspection item 4>

INTERIOR:
- <item>
- <item>
- <item>
- <item>

UNDER THE HOOD:
- <item>
- <item>
- <item>

TEST DRIVE:
- <item — what to feel/listen for>
- <item>
- <item>
- <item>
- <item>

QUESTIONS FOR THE DEALER:
- <question>
- <question>
- <question>
- <question>

MODEL-SPECIFIC RED FLAGS:
- <known issue to specifically check on this model/year>
- <known issue>"""


def claude_cost_of_ownership(car: str) -> str:
    prompt = f"""You are a no-nonsense car-buying advisor. Estimate the realistic 5-year total cost of ownership for: \"{car}\".

Use realistic mainstream assumptions: 12,000 miles/year, average US gas/electricity prices, typical insurance for a 35-year-old driver with a clean record, manufacturer-recommended maintenance.

Return your answer in exactly this format — nothing else:

{CAR_TCO_FORMAT}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def claude_followup(car: str, question: str) -> str:
    prompt = f"""You are a no-nonsense car-buying advisor. The user is researching this car: \"{car}\".

They have a follow-up question:

\"\"\"{question}\"\"\"

Answer directly and concisely (3–6 sentences). Be specific, honest, and practical. If you don't know something for certain, say so. No preamble, no headers — just the answer."""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def claude_test_drive_checklist(car: str) -> str:
    prompt = f"""You are a seasoned car-buying advisor. Generate a thorough but practical inspection and test-drive checklist for: \"{car}\".

Include model-specific red flags — known reliability issues for this make/model/year that buyers should specifically check before buying.

Return your answer in exactly this format — nothing else:

{CAR_CHECKLIST_FORMAT}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def car_news_search(car: str) -> list[dict]:
    topics = [
        ("Review", f"{car} review"),
        ("Recall", f"{car} recall"),
        ("Reliability", f"{car} reliability problems"),
    ]
    seen = set()
    items = []
    for label, q in topics:
        try:
            with DDGS() as ddgs:
                # `news` returns actual articles, not Wikipedia/calendar pages
                results = list(ddgs.news(q, max_results=4, timelimit="y"))
        except Exception:
            results = []
        # fallback to text search if news returns nothing
        if not results:
            try:
                with DDGS() as ddgs:
                    results = [
                        {"title": r.get("title", ""), "url": r.get("href", ""),
                         "body": r.get("body", "")}
                        for r in ddgs.text(q, max_results=4)
                    ]
            except Exception:
                continue
        for r in results:
            href = r.get("url") or r.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)
            items.append({
                "title": r.get("title", ""),
                "url": href,
                "snippet": (r.get("body", "") or r.get("excerpt", "") or "")[:240],
                "topic": label,
            })
            if len(items) >= 10:
                return items
    return items


def claude_compare_cars(car_a: str, car_b: str, car_c: str = "") -> str:
    cars = [c for c in [car_a, car_b, car_c] if c.strip()]
    cars_str = " vs. ".join(cars)
    prompt = f"""You are a no-nonsense car-buying advisor. Compare these vehicles head-to-head: {cars_str}.

Be specific and honest. Call out clear winners in each category. If two are tied, say so.

Return your answer in exactly this format — nothing else:

{CAR_COMPARE_FORMAT}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def claude_deepdive_car(car: str) -> str:
    prompt = f"""You are a no-nonsense car-buying advisor. The user is researching this vehicle: \"{car}\".

Give them everything they need to know before walking into a dealership: real pricing, known problems by model year, what alternatives to cross-shop, and concrete negotiation tips.

Return your answer in exactly this format — nothing else:

{CAR_DEEPDIVE_FORMAT}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Car research routes ───────────────────────────────────────────────────────

@app.route("/cars")
@require_auth
def cars_page():
    return render_template("car.html")


@app.route("/cars/pick", methods=["POST"])
@require_auth
def cars_pick():
    data = request.get_json()
    criteria = (data.get("criteria") or "").strip()
    body_style = (data.get("body_style") or "").strip()
    if not criteria:
        return jsonify({"error": "Tell me what you're looking for first."}), 400
    try:
        return jsonify({"result": claude_pick_cars(criteria, body_style)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/cars/cost", methods=["POST"])
@require_auth
def cars_cost():
    data = request.get_json()
    car = (data.get("car") or "").strip()
    if not car:
        return jsonify({"error": "No car specified."}), 400
    try:
        return jsonify({"result": claude_cost_of_ownership(car)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/cars/followup", methods=["POST"])
@require_auth
def cars_followup():
    data = request.get_json()
    car = (data.get("car") or "").strip()
    question = (data.get("question") or "").strip()
    if not car or not question:
        return jsonify({"error": "Need both a car and a question."}), 400
    try:
        return jsonify({"result": claude_followup(car, question)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/cars/checklist", methods=["POST"])
@require_auth
def cars_checklist():
    data = request.get_json()
    car = (data.get("car") or "").strip()
    if not car:
        return jsonify({"error": "No car specified."}), 400
    try:
        return jsonify({"result": claude_test_drive_checklist(car)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


CAR_FAVORITES_FILE = os.path.join(os.path.dirname(__file__), "data", "car_favorites.json")


def _load_car_favorites():
    try:
        with open(CAR_FAVORITES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_car_favorites(items):
    os.makedirs(os.path.dirname(CAR_FAVORITES_FILE), exist_ok=True)
    with open(CAR_FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


@app.route("/cars/favorites", methods=["GET"])
@require_auth
def cars_favorites_get():
    return jsonify({"favorites": _load_car_favorites()})


@app.route("/cars/favorites", methods=["POST"])
@require_auth
def cars_favorites_set():
    data = request.get_json(silent=True) or {}
    items = data.get("favorites")
    if not isinstance(items, list):
        return jsonify({"error": "favorites must be a list"}), 400
    cleaned = []
    seen = set()
    for x in items:
        if not isinstance(x, str):
            continue
        name = x.strip()[:120]
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            cleaned.append(name)
        if len(cleaned) >= 100:
            break
    _save_car_favorites(cleaned)
    return jsonify({"favorites": cleaned})


LISTING_SITES = [
    ("autotrader.com",  "AutoTrader"),
    ("cars.com",        "Cars.com"),
    ("cargurus.com",    "CarGurus"),
    ("carvana.com",     "Carvana"),
    ("edmunds.com",     "Edmunds"),
    ("truecar.com",     "TrueCar"),
    ("carmax.com",      "CarMax"),
]


def listings_search(query: str) -> list[dict]:
    """Run a site-restricted DDG search across the major listings aggregators
    and return up to 12 deduped results."""
    sites_clause = " OR ".join(f"site:{d}" for d, _ in LISTING_SITES)
    full_query = f"{query} ({sites_clause})"
    seen = set()
    items = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(full_query, max_results=20):
                href = r.get("href", "")
                if not href or href in seen:
                    continue
                seen.add(href)
                source = next((label for dom, label in LISTING_SITES if dom in href), "")
                if not source:
                    continue
                items.append({
                    "title":   r.get("title", "")[:160],
                    "url":     href,
                    "snippet": (r.get("body", "") or "")[:240],
                    "source":  source,
                })
                if len(items) >= 12:
                    break
    except Exception:
        pass
    return items


@app.route("/cars/listings", methods=["POST"])
@require_auth
def cars_listings():
    data = request.get_json() or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Build a search first."}), 400
    return jsonify({"results": listings_search(query)})


@app.route("/cars/news", methods=["POST"])
@require_auth
def cars_news():
    data = request.get_json()
    car = (data.get("car") or "").strip()
    if not car:
        return jsonify({"error": "No car specified."}), 400
    try:
        return jsonify({"results": car_news_search(car)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/cars/compare", methods=["POST"])
@require_auth
def cars_compare():
    data = request.get_json()
    car_a = (data.get("car_a") or "").strip()
    car_b = (data.get("car_b") or "").strip()
    car_c = (data.get("car_c") or "").strip()
    if not car_a or not car_b:
        return jsonify({"error": "Enter at least two cars to compare."}), 400
    try:
        return jsonify({"result": claude_compare_cars(car_a, car_b, car_c)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/cars/deepdive", methods=["POST"])
@require_auth
def cars_deepdive():
    data = request.get_json()
    car = (data.get("car") or "").strip()
    if not car:
        return jsonify({"error": "Enter a car to research."}), 400
    try:
        return jsonify({"result": claude_deepdive_car(car)})
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


@app.route("/ping")
def ping():
    return Response("pong", 200)


@app.route("/briefing", methods=["GET", "POST"])
def briefing():
    secret = request.args.get("secret")
    if not BRIEFING_SECRET or secret != BRIEFING_SECRET:
        return Response("Unauthorized", 401)
    weather = get_weather()
    news = get_auburn_news()
    html = generate_briefing_html(weather, news)
    today = datetime.now(CENTRAL).strftime("%A, %B %d")
    send_briefing_email(f"Good Morning, Matthew — {today}", html)
    return jsonify({"status": "sent"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=8080)
