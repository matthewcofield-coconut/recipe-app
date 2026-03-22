import os
import anthropic
import requests
import spotipy
from youtube_parser import parse_history
from spotipy.oauth2 import SpotifyOAuth
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import functools
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, Response

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-dev-key")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "changeme")

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
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8080/callback"
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


def claude_pick_tracks(vibe: str) -> list[dict]:
    prompt = f"""The user wants a Spotify playlist with this vibe: "{vibe}"

Return exactly 20 tracks that fit this vibe. For each track return the exact song title and the exact artist name, separated by a pipe character.

Format (one per line, nothing else):
track title | artist name

Be specific — use the correct artist so there is no ambiguity with other songs of the same name."""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    tracks = []
    for line in msg.content[0].text.strip().split("\n"):
        if "|" in line:
            parts = line.split("|", 1)
            tracks.append({"title": parts[0].strip(), "artist": parts[1].strip()})
    return tracks


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


@app.route("/youtube")
@require_auth
def youtube_page():
    return render_template("youtube.html")


@app.route("/youtube/data")
def youtube_data():
    data = parse_history()
    return jsonify(data)


@app.route("/youtube/recommend", methods=["POST"])
def youtube_recommend():
    data = parse_history()
    top = [ch for ch, _ in data["top_channels"][:15]]
    prompt = f"""Based on these YouTube channels a user watches most frequently:
{', '.join(top)}

Suggest 8 YouTube creators or channels they would likely enjoy but may not have discovered yet.
For each, give the channel name and one sentence on why they'd like it based on the above viewing habits.

Format exactly like this, nothing else:
CHANNEL: <name>
WHY: <one sentence reason>

Repeat for all 8."""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return jsonify({"recommendations": msg.content[0].text.strip()})


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=8080)
