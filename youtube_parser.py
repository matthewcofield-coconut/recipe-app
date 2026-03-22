import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from bs4 import BeautifulSoup

HISTORY_PATH = "/Users/matthewcofield/Projects/recipe-app/data/Takeout/YouTube and YouTube Music/history/watch-history.html"

_cache = None

def parse_history():
    global _cache
    if _cache is not None:
        return _cache

    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "lxml")

    entries = soup.find_all("div", class_="outer-cell")

    videos = []
    channel_counts = Counter()
    month_counts = defaultdict(int)
    hour_counts = defaultdict(int)
    dow_counts = defaultdict(int)

    for entry in entries:
        cell = entry.find("div", class_="content-cell")
        if not cell:
            continue

        links = cell.find_all("a")
        if len(links) < 2:
            continue

        title = links[0].get_text(strip=True)
        channel = links[1].get_text(strip=True)
        text = cell.get_text(separator="\n")

        # Parse date
        date_str = None
        for line in text.split("\n"):
            line = line.strip()
            if re.match(r"[A-Z][a-z]+ \d+, \d{4}", line):
                date_str = line
                break

        dt = None
        if date_str:
            try:
                dt = datetime.strptime(date_str[:20].strip(), "%b %d, %Y, %I:%M:%S")
            except Exception:
                try:
                    dt = datetime.strptime(date_str[:12].strip(), "%b %d, %Y")
                except Exception:
                    pass

        if dt:
            month_counts[dt.strftime("%Y-%m")] += 1
            hour_counts[dt.hour] += 1
            dow_counts[dt.strftime("%A")] += 1

        channel_counts[channel] += 1
        videos.append({"title": title, "channel": channel, "date": date_str})

    top_channels = channel_counts.most_common(20)

    # Sort months
    sorted_months = sorted(month_counts.items())

    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_data = [(d, dow_counts.get(d, 0)) for d in dow_order]

    _cache = {
        "total": len(videos),
        "unique_channels": len(channel_counts),
        "top_channels": top_channels,
        "month_counts": sorted_months,
        "hour_counts": [(h, hour_counts.get(h, 0)) for h in range(24)],
        "dow_counts": dow_data,
        "recent": videos[:10],
    }
    return _cache
