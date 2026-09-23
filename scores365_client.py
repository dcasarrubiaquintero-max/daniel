"""Cliente 365Scores para EdgeBet.
Fuente observada: webws.365scores.com/web. No usa una API key.
Se mantiene separado del motor principal para poder activar 365Scores solo
cuando la respuesta del endpoint sea válida, sin fabricar datos.
"""
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://webws.365scores.com/web"
COMMON = {"appTypeId": 5, "langId": 29, "timezoneName": "America/Bogota"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.365scores.com/",
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
    return get_json("game/", gameId=game_id).get("game", {})

def _stat_number(stats, *keys):\n    for k in keys:\n        if k in stats:\n            try: return float(stats[k])\n            except (TypeError, ValueError): pass\n    return None\n\ndef normalize_player_stats(game_id):\n    raw = player_stats(game_id)\n    out=[]\n    for team in raw.get("teams",[]):\n        for p in team.get("players",[]):\n            st=p.get("stats",{})\n            out.append({\n                "game_id":game_id,"team":team.get("team"),"player_id":p.get("player_id"),\n                "name":p.get("name"),"starter":p.get("starter"),"rating":p.get("rating"),\n                "shots":_stat_number(st,"shots","totalShots","Total Shots"),\n                "shots_on_target":_stat_number(st,"shotsOnTarget","shots on target","Shots on target"),\n                "fouls_committed":_stat_number(st,"foulsCommitted","fouls","Fouls"),\n                "minutes":_stat_number(st,"minutes","Minutes"),\n                "xg":_stat_number(st,"xG","xg","Expected Goals")\n            })\n    return out\n\ndef player_signal(history, line, field, market):\n    vals=[x[field] for x in history if isinstance(x.get(field),(int,float))]\n    if len(vals)<5: return None\n    hits=sum(v>line for v in vals)\n    rate=hits/len(vals)\n    avg=sum(vals)/len(vals)\n    if rate<0.70: return None\n    return {"market":market,"prob":round(rate*100,1),"avg":round(avg,2),"sample":len(vals)}\n\ndef player_stats(game_id):
    g = game(game_id)
    members = {m.get("id"): m for m in g.get("members", [])}
    teams = []
    for side in ("homeCompetitor", "awayCompetitor"):
        c = g.get(side, {}) or {}
        players = []
        for row in (c.get("lineups", {}) or {}).get("members", []) or []:
            info = members.get(row.get("id"), {})
            raw = row.get("stats") or []
            by_name = {x.get("name"): x.get("value") for x in raw if x.get("name")}
            by_type = {x.get("type"): x.get("value") for x in raw if x.get("type") is not None}
            players.append({
                "player_id": row.get("id"),
                "name": info.get("name") or info.get("shortName"),
                "starter": row.get("status") == 1 or row.get("statusText") == "Starting",
                "rating": row.get("ranking"),
                "stats": by_name,
                "stats_by_type": by_type,
            })
        teams.append({"team_id": c.get("id"), "team": c.get("name"), "players": players})
    return {"game_id": game_id, "teams": teams}

def shots(game_id):
    g = game(game_id)
    events = (g.get("chartEvents") or {}).get("events") or []
    members = {m.get("id"): m.get("name") for m in g.get("members", [])}
    out = []
    for e in events:
        out.append({
            "player": members.get(e.get("playerId")),
            "player_id": e.get("playerId"),
            "competitor": e.get("competitorNum"),
            "xg": e.get("xg"),
            "xgot": e.get("xgot"),
            "outcome": (e.get("outcome") or {}).get("name"),
            "minute": e.get("time"),
        })
    return {"game_id": game_id, "shots": out}

def lineups(game_id):
    g = game(game_id)
    members = {m.get("id"): m for m in g.get("members", [])}
    result = []
    for side in ("homeCompetitor", "awayCompetitor"):
        c = g.get(side, {}) or {}
        lu = c.get("lineups") or {}
        result.append({
            "team": c.get("name"),
            "status": lu.get("status"),
            "players": [{
                "player_id": m.get("id"),
                "name": (members.get(m.get("id")) or {}).get("name"),
                "starter": m.get("status") == 1 or m.get("statusText") == "Starting",
                "position": (m.get("position") or {}).get("name"),
            } for m in lu.get("members", []) or []]
        })
    return {"game_id": game_id, "teams": result}
