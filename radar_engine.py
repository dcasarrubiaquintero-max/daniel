import json, math, os, re, statistics, unicodedata
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

try:
    from scores365_client import find_match, fixture_rows, normalize_player_stats, lineups, norm
except Exception:
    find_match = fixture_rows = normalize_player_stats = lineups = None

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
PLAYER_OVERRIDES = {
    ("Australia","Brazil","25/09/2026"): ("Raphinha más de 1+ tiros", 0.97, "Statz: 97% de acierto en 37 partidos."),
    ("South Korea","Ecuador","24/09/2026"): ("Hyeon-gyu Oh más de 1+ tiros", 0.93, "Statz: 93% de acierto en 46 partidos."),
    ("United States","Peru","26/09/2026"): ("Folarin Balogun más de 1+ tiros", 0.92, "Statz: 92% de acierto en 48 partidos."),
    ("Canada","Chile","26/09/2026"): ("Ali Ahmed más de 1+ tiros", 0.86, "Statz: 86% de acierto en 36 partidos."),
    ("Russia","Iran","29/09/2026"): ("Maksim Glushenkov más de 1+ tiros", 0.94, "Statz: 94% de acierto en 35 partidos."),
}

LEAGUES = {
    "Premier League":"eng.1", "LaLiga":"esp.1", "Serie A":"ita.1",
    "Bundesliga":"ger.1", "Ligue 1":"fra.1", "Champions League":"uefa.champions",
    "Europa League":"uefa.europa", "Nations League":"uefa.nations", "Amistosos":"fifa.friendly",
}
UA = "EdgeBet-AI/3.1"
_365_match_budget = 50

def get_json(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def parse_num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    m = re.search(r"-?\d+(?:[\.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None

def poisson_over(lam, threshold):
    if lam <= 0: return 0.0
    k = int(math.floor(threshold))
    return max(0.0, min(1.0, 1 - sum(math.exp(-lam)*lam**i/math.factorial(i) for i in range(k+1))))

def hit_rate(vals, line):
    return sum(1 for x in vals if x > line) / len(vals) if vals else 0.0

def summary(event_id, league):
    try:
        d = get_json(f"{BASE}/{LEAGUES[league]}/summary?event={event_id}")
    except Exception:
        return None
    comps = d.get("header", {}).get("competitions", [])
    if not comps: return None
    teams = comps[0].get("competitors", [])
    if len(teams) != 2: return None
    home = next((x for x in teams if x.get("homeAway") == "home"), teams[0])
    away = next((x for x in teams if x.get("homeAway") == "away"), teams[-1])
    hs, aws = parse_num(home.get("score")), parse_num(away.get("score"))
    if hs is None or aws is None: return None
    return {
        "home": home.get("team", {}).get("displayName"),
        "away": away.get("team", {}).get("displayName"),
        "gf": hs, "ga": aws,
        "stats": d.get("boxscore", {}).get("teams", []),
        "players": d.get("boxscore", {}).get("players", []),
    }

def extract_player_rows(summary_data, team_name):
    out = []
    players = summary_data.get("boxscore", {}).get("players", []) or []
    for block in players:
        team = (block.get("team") or {}).get("displayName") or team_name
        for group in block.get("statistics", []) or []:
            labels = group.get("labels") or group.get("names") or []
            for athlete in group.get("athletes", []) or []:
                ar = athlete.get("athlete") or {}
                name = ar.get("displayName") or ar.get("shortName")
                raw = athlete.get("stats") or athlete.get("statistics") or []
                vals = {}
                if name and isinstance(raw, list):
                    for label, val in zip(labels, raw):
                        num = parse_num(val)
                        if num is not None:
                            vals[str(label).lower()] = num
                    if vals:
                        out.append({"name": name, "stats": vals, "team": team})
    return out

def extract_team_row(s, team_name):
    for b in s.get("stats", []) or []:
        if b.get("team", {}).get("displayName") != team_name: continue
        vals = {}
        for x in b.get("statistics", []) or []:
            label = (x.get("name") or x.get("displayName") or "").lower()
            val = parse_num(x.get("displayValue", x.get("value")))
            if val is not None: vals[label] = val
        def pick(*terms):
            for k, v in vals.items():
                if any(t in k for t in terms): return v
            return None
        return {
            "gf": s["gf"], "ga": s["ga"], "corners": pick("corner"),
            "shots": pick("total shots", "shots"), "sot": pick("shots on target"),
            "cards": pick("yellow card"), "fouls": pick("foul"),
        }
    return None

def past_team_events(league, team_id, limit=5):
    try: d = get_json(f"{BASE}/{LEAGUES[league]}/teams/{team_id}/schedule")
    except Exception: return []
    ev = []
    for e in d.get("events", []):
        if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed"):
            ev.append(e)
    ev.sort(key=lambda x: x.get("date", ""), reverse=True)
    return ev[:limit]

def row_for_event(e, league, team_id):
    s = summary(e.get("id"), league)
    if not s: return None
    comps = e.get("competitions", [{}])[0].get("competitors", [])
    home_id = comps[0].get("team", {}).get("id") if comps else None
    side_home = str(home_id) == str(team_id)
    team, opp = (s["home"], s["away"]) if side_home else (s["away"], s["home"])
    row = extract_team_row(s, team)
    if not row: return None
    cards, fouls = [], []
    for b in s.get("stats", []) or []:
        vals = {}
        for x in b.get("statistics", []) or []:
            label = (x.get("name") or x.get("displayName") or "").lower()
            val = parse_num(x.get("displayValue", x.get("value")))
            if val is not None: vals[label] = val
        for k, v in vals.items():
            if "yellow card" in k: cards.append(v); break
        for k, v in vals.items():
            if "foul" in k: fouls.append(v); break
    row["total_cards"] = sum(cards) if len(cards) == 2 else None
    row["total_fouls"] = sum(fouls) if len(fouls) == 2 else None
    row["opp"], row["venue"] = opp, ("home" if side_home else "away")
    row["player_rows"] = extract_player_rows({"boxscore": {"players": s.get("players", [])}}, team)
    return row

def team_rows(league, team_id):
    return [r for e in past_team_events(league, team_id, 5) if (r := row_for_event(e, league, team_id))]

def player_candidates(rows, team_name):
    by = {}
    for r in rows:
        for p in r.get("player_rows", []):
            if p.get("team") != team_name: continue
            st = p.get("stats", {})
            shots = next((v for k,v in st.items() if k in ("shots","total shots","totalshots","sh","shot")), None)
            sot = next((v for k,v in st.items() if k in ("shots on target","shotsontarget","sot","sog","shots on goal")), None)
            fouls = next((v for k,v in st.items() if k in ("fouls","fouls committed","foulscommitted","fc")), None)
            z = by.setdefault(p.get("name"), {"shots":[],"sot":[],"fouls":[]})
            if shots is not None: z["shots"].append(shots)
            if sot is not None: z["sot"].append(sot)
            if fouls is not None: z["fouls"].append(fouls)
    return by

def signals_365(match):
    if not (fixture_rows and normalize_player_stats and find_match): return []
    try:
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=75)).isoformat()
        end = (today - timedelta(days=1)).isoformat()
        target_home, target_away = norm(match["home"]), norm(match["away"])
        hist = fixture_rows(match["league"], start, end, results=True)
        relevant = [fx for fx in hist if fx["home_norm"] in (target_home, target_away) or fx["away_norm"] in (target_home, target_away)]
        relevant = relevant[-10:]
        player_history = {}
        for fx in relevant:
            target_teams = set()
            if fx["home_norm"] in (target_home, target_away): target_teams.add(fx["home_norm"])
            if fx["away_norm"] in (target_home, target_away): target_teams.add(fx["away_norm"])
            for p in normalize_player_stats(fx["id"]):
                name = p.get("name")
                if not name or norm(p.get("team")) not in target_teams: continue
                player_history.setdefault(name, []).append(p)
        upcoming_365 = find_match(match["league"], match["home"], match["away"], match.get("date_iso", match["date"]))
        starters = set()
        if upcoming_365:
            for t in (lineups(upcoming_365["id"]).get("teams", []) if lineups else []):
                for p in t.get("players", []):
                    if p.get("starter"): starters.add(p.get("name"))
        out = []
        for name, hist_rows in player_history.items():
            usable = [x for x in hist_rows if isinstance(x.get("minutes"), (int,float)) and x["minutes"] >= 60]
            if len(usable) < 5: continue
            for field, line, label in [
                ("shots", 1.5, "tiros"), ("shots_on_target", 0.5, "tiros a puerta"),
                ("fouls_committed", 1.5, "faltas cometidas")
            ]:
                vals = [x[field] for x in usable if isinstance(x.get(field), (int,float))]
                if len(vals) < 5: continue
                hr = hit_rate(vals, line)
                if hr < .70: continue
                if starters and name not in starters: continue
                avg = statistics.mean(vals)
                out.append({
                    "market": f"{name} más de {line} {label}",
                    "prob": round(hr*100, 1),
                    "why": f"365Scores: {sum(v>line for v in vals)}/{len(vals)} supera la línea; media {avg:.2f}; muestra de {len(vals)} partidos con 60+ min.",
                    "source": "365Scores",
                })
        return out
    except Exception:
        return []
def slugify_team(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii","ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+","-",s).strip("-")

_STATZ_CACHE = {}
_STATZ_BUDGET = 16
_STATZ_CALLS = 0

def statz_player_pick(home, away):
    global _STATZ_CALLS
    if _STATZ_CALLS >= _STATZ_BUDGET: return None
    for team, opp in [(home,away),(away,home)]:
        try:
            slug = slugify_team(team)
            if slug in _STATZ_CACHE:
                text = _STATZ_CACHE[slug]
            else:
                if _STATZ_CALLS >= _STATZ_BUDGET: return None
                _STATZ_CALLS += 1
                url = f"https://statz.ai/team/{slug}"
                req = Request(url, headers={"User-Agent":"Mozilla/5.0 EdgeBet-AI/3.1","Accept":"text/html"})
                with urlopen(req, timeout=6) as r:
                    html = r.read().decode("utf-8","ignore")
                text = re.sub(r"<[^>]+>"," ",html)
                text = re.sub(r"\s+"," ",text)
                _STATZ_CACHE[slug] = text
            pos = text.lower().find(f"{team} best bet builder picks vs {opp}".lower())
            if pos < 0: continue
            section = text[pos:pos+1800]
            pat = re.compile(r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'’ -]{1,55}?)\s*-\s*(\d+\+)\s+(Shots|SOT|Fouls|Fouls Drawn)\s*\((\d+)% hit rate over (\d+) games\)", re.I)
            picks = []
            for m in pat.finditer(section):
                player,line,market,rate,sample=m.groups()
                rank={"shots":0,"sot":1,"fouls":2,"fouls drawn":3}.get(market.lower(),9)
                picks.append((rank,-int(rate),player.strip(),line,market.lower(),int(rate),int(sample)))
            if picks:
                _,_,player,line,market,rate,sample=sorted(picks)[0]
                label={"shots":"tiros","sot":"tiros a puerta","fouls":"faltas cometidas","fouls drawn":"faltas recibidas"}[market]
                return {"market":f"{player} más de {line} {label}","prob":rate/100,"why":f"Statz: {rate}% de acierto en {sample} partidos.","source":"Statz"}
        except Exception:
            pass
    return None


def signal(m, a, b):
    rows = a + b
    if len(a) < 4 or len(b) < 4:
        return {"league":m["league"],"match":f'{m["home"]} vs {m["away"]}',"date":m["date"],"time":m["time"],
                "market":"SIN SEÑAL — datos insuficientes","prob":0,"confidence":"DATOS INSUFICIENTES",
                "why":"Se requieren al menos 4 partidos recientes por equipo para evaluar goles, BTTS y córners sin forzar una recomendación.",
                "source":"Radar","sample":len(rows),"generated_at":datetime.now(timezone.utc).isoformat()}

    candidates = []

    # 1) Más de 1.5 goles: prioridad principal cuando ambos equipos muestran consistencia.
    goals = [r["gf"] + r["ga"] for r in rows]
    hr15 = hit_rate(goals, 1.5)
    lam15 = statistics.mean(goals) if goals else 0
    if len(goals) >= 8 and hr15 >= 0.70:
        p = min(0.96, max(0.55, 0.55*hr15 + 0.45*poisson_over(lam15, 1.5)))
        candidates.append((p, "Más de 1.5 goles",
            f"Se superó 1.5 goles en {sum(v>1.5 for v in goals)}/{len(goals)} partidos; media {lam15:.2f}.", "ESPN"))

    # 2) Ambos marcan: exige evidencia de ambos lados, no solo muchos goles.
    btts_home = sum(1 for r in a if r["gf"] > 0 and r["ga"] > 0) / len(a)
    btts_away = sum(1 for r in b if r["gf"] > 0 and r["ga"] > 0) / len(b)
    btts = (btts_home + btts_away) / 2
    if btts >= 0.62:
        candidates.append((btts, "Ambos equipos marcan",
            f"BTTS en {btts_home*100:.0f}% de los últimos {len(a)} del {m['home']} y {btts_away*100:.0f}% de los últimos {len(b)} del {m['away']}.", "ESPN"))

    # 3) Córners totales: solo si la muestra y la consistencia respaldan la línea.
    corners = [r["corners"] for r in rows if r.get("corners") is not None]
    if len(corners) >= 8:
        for line, minimum in [(7.5,0.68),(8.5,0.64),(9.5,0.60),(10.5,0.56)]:
            hr = hit_rate(corners, line)
            lam = statistics.mean(corners)
            p = 0.50*hr + 0.50*poisson_over(lam, line)
            if hr >= minimum and p >= 0.62:
                candidates.append((p, f"Más de {line} córners",
                    f"Se superó la línea en {sum(v>line for v in corners)}/{len(corners)} partidos; media {lam:.1f} córners.", "ESPN"))

    # 4) Córners por equipo: mercado individual de córners, sin usar tiros de jugadores.
    for name, rs in [(m["home"], a), (m["away"], b)]:
        vals = [r["corners"] for r in rs if r.get("corners") is not None]
        if len(vals) >= 4:
            for line, minimum in [(3.5,0.70),(4.5,0.64),(5.5,0.58)]:
                hr = hit_rate(vals, line)
                lam = statistics.mean(vals)
                p = 0.50*hr + 0.50*poisson_over(lam, line)
                if hr >= minimum and p >= 0.62:
                    candidates.append((p, f"{name} más de {line} córners",
                        f"{name}: supera la línea en {sum(v>line for v in vals)}/{len(vals)} partidos; media {lam:.1f} córners.", "ESPN"))

    if not candidates:
        return {"league":m["league"],"match":f'{m["home"]} vs {m["away"]}',"date":m["date"],"time":m["time"],
                "market":"SIN SEÑAL","prob":0,"confidence":"SIN SEÑAL",
                "why":"Los datos recientes no superan el filtro mínimo para goles, ambos marcan o córners. No se fuerza una apuesta.",
                "source":"Radar","sample":len(rows),"generated_at":datetime.now(timezone.utc).isoformat()}

    # Prioridad: BTTS y goles si tienen evidencia claramente superior; después córners.
    p, market, why, source = max(candidates, key=lambda x:x[0])
    return {"league":m["league"],"match":f'{m["home"]} vs {m["away"]}',"date":m["date"],"time":m["time"],
            "market":market,"prob":round(p*100,1),
            "confidence":"ALTA" if p >= .75 else "MEDIA-ALTA" if p >= .68 else "MEDIA",
            "why":why,"source":source,"sample":len(rows),"generated_at":datetime.now(timezone.utc).isoformat()}

