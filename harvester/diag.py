#!/usr/bin/env python3
"""Cloud-IP endpoint probe. Run from GitHub Actions to see which Roblox
discovery/metadata endpoints actually work from a datacenter IP (vs being
rate-limited/blocked). Prints status codes and result counts; never fails."""

import time
import uuid
import requests

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
})
SID = str(uuid.uuid4())


def probe(label, url, params=None, repeat=1):
    """Hit an endpoint `repeat` times; report status codes + a size hint."""
    codes = []
    sample = ""
    for i in range(repeat):
        try:
            r = S.get(url, params=params, timeout=20)
            codes.append(r.status_code)
            if r.status_code == 200 and not sample:
                sample = r.text[:160].replace("\n", " ")
        except Exception as e:
            codes.append("ERR:" + type(e).__name__)
        time.sleep(1.0)
    print(f"\n## {label}")
    print(f"   url: {url}")
    print(f"   codes: {codes}")
    if sample:
        print(f"   sample: {sample}")


print("=== Roblox endpoint probe from", end=" ")
try:
    ip = S.get("https://api.ipify.org", timeout=10).text
    print(f"IP {ip} ===")
except Exception:
    print("unknown IP ===")

# 1. omni-search (our current discovery source) - hammer it to see throttling
probe("omni-search 'tycoon' (x5, throttle check)",
      "https://apis.roblox.com/search-api/omni-search",
      {"searchQuery": "tycoon", "sessionId": SID, "pageType": "Game"}, repeat=5)

# 2. explore-api charts (sorts) - CDN-ish, may survive datacenter IPs
probe("explore-api/get-sorts",
      "https://apis.roblox.com/explore-api/v1/get-sorts", {"sessionId": SID})

# 3. games metadata batch (our enrich endpoint) - known to work locally
probe("games/v1 metadata batch (x5, throttle check)",
      "https://games.roblox.com/v1/games",
      {"universeIds": "1686885941,920587237,245662005,994732206,1686885941"},
      repeat=5)

# 4. games list by sort token (legacy)
probe("games/v1/games/list (legacy)",
      "https://games.roblox.com/v1/games/list",
      {"model.keyword": "obby", "model.maxRows": 25})

# 5. catalog/search-style alternative
probe("games-api charts get-sort-content",
      "https://apis.roblox.com/explore-api/v1/get-sort-content",
      {"sessionId": SID, "sortId": "top-trending"})

def throttle_rate(spacing):
    """Send 8 omni-search requests `spacing` seconds apart; report success rate."""
    ok = 0
    codes = []
    for i in range(8):
        try:
            r = S.get("https://apis.roblox.com/search-api/omni-search",
                      params={"searchQuery": "obby" + str(i), "sessionId": SID,
                              "pageType": "Game"}, timeout=20)
            codes.append(r.status_code)
            if r.status_code == 200:
                ok += 1
        except Exception as e:
            codes.append("ERR")
        if i < 7:
            time.sleep(spacing)
    print(f"\n## omni sustainable rate @ {spacing}s spacing: {ok}/8 ok  {codes}")


print("\n--- omni-search throttle window test ---")
throttle_rate(3)
throttle_rate(8)
throttle_rate(15)

print("\n=== probe complete ===")
