"""Optional 365Scores data layer for EdgeBet.

Uses the web endpoint observed on 365Scores pages. It is intentionally defensive:
if an endpoint changes or returns an unexpected shape, callers receive an empty
result rather than fabricated statistics.
"""
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://webws.365scores.com/web"
COMMON = {"appTypeId": 5, "langId": 29, "timezoneName": "America/Bogota"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.365scores.com/",
}

# Verified from 365Scores league URLs.
COMPETITIONS = {
    "Premier League": 7,
    "LaLiga": 11,
    "Serie A": 17,
    "Bundesliga": 25,
    "Ligue 1": 35,
    "Champions League": 572,
    "Europa League": 573,
    "Nations League": 7016,
    "Amistosos": 570,
}

def get_json(path, **params):
    q = dict(COMMON)
    q.update(params)
    url = BASE.rstrip("/") + "/" + path.lstrip("/") + "?" + urlencode(q)
    req = Request(url, headers=HEADERS)
    last = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise last

def game(game_id):
    data = get_json("game/", gameId=game_id)
    return data.get("game", {}) or {}

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    aliases = {
        "paris saint germain": "psg", "paris sg": "psg",
        "inter milan": "inter", "internazionale": "inter",
        "ac milan": "milan", "fc barcelona": "barcelona",
        "atletico madrid": "atletico", "manchester united": "man united",
        "manchester city": "man city", "tottenham hotspur": "tottenham",
        "bayern munich": "bayern", "borussia dortmund": "dortmund",
        "real betis": "betis", "real sociedad": "sociedad",
    }
    return aliases.get(s, s)

def team_pair(g):
    h = g.get("homeCompetitor") or {}
    a = g.get("awayCompetitor") or {}
    return norm(h.get("name")), norm(a.get("name"))

def fixture_rows(competition, date_from=None, date_to=None, results=False):
    cid = COMPETITIONS.get(competition)
    if not cid:
        return []
    if date_from is None:
        date_from = datetime.now(timezone.utc).date().isoformat()
    if date_to is None:
        date_to = date_from
    path = "games/results/" if results else "games/fixtures/"
    try:
        data = get_json(path, competitions=cid, startDate=date_from, endDate=date_to)
    except Exception:
        # Some deployments use a single date parameter.
        try:
            data = get_json(path, competitions=cid, date=date_from)
        except Exception:
            return []
    raw = data.get("games") or data.get("events") or data.get("matches") or []
    out = []
    for item in raw:
        g = item.get("game") if isinstance(item, dict) and item.get("game") else item
        if not isinstance(g, dict):
            continue
        gid = g.get("id") or g.get("gameId")
        h = g.get("homeCompetitor") or {}
        a = g.get("awayCompetitor") or {}
        if gid and h.get("name") and a.get("name"):
            out.append({
                "id": str(gid), "home": h.get("name"), "away": a.get("name"),
                "home_norm": norm(h.get("name")), "away_norm": norm(a.get("name")),
                "competition": competition, "raw": g,
            })
    return out

def find_match(competition, home, away, date):
    target_h, target_a = norm(home), norm(away)
    rows = fixture_rows(competition, date, date)
    exact = [x for x in rows if x["home_norm"] == target_h and x["away_norm"] == target_a]
    if exact:
        return exact[0]
    # Allow provider naming differences while preserving home/away orientation.
    for x in rows:
        if (target_h in x["home_norm"] or x["home_norm"] in target_h) and (target_a in x["away_norm"] or x["away_norm"] in target_a):
            return x
    return None

def _number(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v or ""))
    return float(m.group(0).replace(",", ".")) if m else None

def _stat_number(stats, *keys):
    if not isinstance(stats, dict):
        return None
    lower = {str(k).lower(): v for k, v in stats.items()}
    for key in keys:
        v = lower.get(str(key).lower())
        n = _number(v)
        if n is not None:
            return n
    return None

def player_stats(game_id):
    g = game(game_id)
    members = {m.get("id"): m for m in g.get("members", []) if isinstance(m, dict)}
    teams = []
    for side in ("homeCompetitor", "awayCompetitor"):
        c = g.get(side) or {}
        players = []
        lineup = c.get("lineups") or {}
        for row in lineup.get("members", []) or []:
            info = members.get(row.get("id"), {})
            raw = row.get("stats") or []
            by_name = {x.get("name"): x.get("value") for x in raw if isinstance(x, dict) and x.get("name")}
            by_type = {x.get("type"): x.get("value") for x in raw if isinstance(x, dict) and x.get("type") is not None}
            players.append({
                "player_id": row.get("id"),
                "name": info.get("name") or info.get("shortName"),
                "starter": row.get("status") == 1 or row.get("statusText") == "Starting",
                "rating": _number(row.get("ranking")),
                "stats": by_name,
                "stats_by_type": by_type,
            })
        teams.append({"team_id": c.get("id"), "team": c.get("name"), "players": players})
    return {"game_id": str(game_id), "teams": teams}

def normalize_player_stats(game_id):
    raw = player_stats(game_id)
    out = []
    for team in raw.get("teams", []):
        for p in team.get("players", []):
            st = p.get("stats", {})
            out.append({
                "game_id": str(game_id), "team": team.get("team"),
                "player_id": p.get("player_id"), "name": p.get("name"),
                "starter": p.get("starter"), "rating": p.get("rating"),
                "shots": _stat_number(st, "shots", "totalShots", "Total Shots"),
                "shots_on_target": _stat_number(st, "shotsOnTarget", "shots on target", "Shots on target"),
                "fouls_committed": _stat_number(st, "foulsCommitted", "fouls", "Fouls"),
                "minutes": _stat_number(st, "minutes", "Minutes"),
                "xg": _stat_number(st, "xG", "xg", "Expected Goals"),
            })
    return out

def lineups(game_id):
    g = game(game_id)
    members = {m.get("id"): m for m in g.get("members", []) if isinstance(m, dict)}
    result = []
    for side in ("homeCompetitor", "awayCompetitor"):
        c = g.get(side) or {}
        lu = c.get("lineups") or {}
        result.append({
            "team": c.get("name"),
            "status": lu.get("status"),
            "players": [{
                "player_id": m.get("id"),
                "name": (members.get(m.get("id")) or {}).get("name"),
                "starter": m.get("status") == 1 or m.get("statusText") == "Starting",
                "position": (m.get("position") or {}).get("name"),
            } for m in lu.get("members", []) or []],
        })
    return {"game_id": str(game_id), "teams": result}

def shots(game_id):
    g = game(game_id)
    members = {m.get("id"): m.get("name") for m in g.get("members", []) if isinstance(m, dict)}
    events = (g.get("chartEvents") or {}).get("events") or []
    return {"game_id": str(game_id), "shots": [{
        "player": members.get(e.get("playerId")),
        "player_id": e.get("playerId"),
        "competitor": e.get("competitorNum"),
        "xg": _number(e.get("xg")),
        "xgot": _number(e.get("xgot")),
        "outcome": (e.get("outcome") or {}).get("name"),
        "minute": e.get("time"),
    } for e in events]}

def player_signal(history, line, field, market):
    vals = [x[field] for x in history if isinstance(x.get(field), (int, float))]
    if len(vals) < 5:
        return None
    hits = sum(v > line for v in vals)
    rate = hits / len(vals)
    if rate < 0.70:
        return None
    return {
        "market": market, "prob": round(rate * 100, 1),
        "avg": round(sum(vals) / len(vals), 2), "sample": len(vals),
    }
