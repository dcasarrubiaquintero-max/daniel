import json, math, os, re, statistics, unicodedata
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

try:
    from scores365_client import find_match, fixture_rows, normalize_player_stats, lineups, norm
except Exception:
    find_match = fixture_rows = normalize_player_stats = lineups = None

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
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

def statz_player_pick(home, away):
    for team, opp in [(home,away),(away,home)]:
        try:
            url = f"https://statz.ai/team/{slugify_team(team)}"
            req = Request(url, headers={"User-Agent":"Mozilla/5.0 EdgeBet-AI/3.1","Accept":"text/html"})
            with urlopen(req, timeout=8) as r:
                html = r.read().decode("utf-8","ignore")
            text = re.sub(r"<[^>]+>"," ",html)
            text = re.sub(r"\s+"," ",text)
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
    if len(rows) < 6: return None
    candidates = []
    goals = [r["gf"] + r["ga"] for r in rows]
    lam = statistics.mean(goals)
    for line, minp, minhr, label in [(1.5,.78,.70,"Más de 1.5 goles"),(2.5,.66,.55,"Más de 2.5 goles"),(3.5,.60,.45,"Más de 3.5 goles")]:
        p, hr = poisson_over(lam, line), hit_rate(goals, line)
        if p >= minp and hr >= minhr:
            candidates.append((p, label, f"Media reciente {lam:.2f}; supera {line} en {hr*100:.0f}% de la muestra.", "ESPN"))
    btts = sum(1 for r in rows if r["gf"] > 0 and r["ga"] > 0) / len(rows)
    if btts >= .62: candidates.append((btts, "Ambos equipos marcan", f"BTTS en {btts*100:.0f}% de los últimos {len(rows)} partidos.", "ESPN"))
    for field, line, label, threshold in [("corners",8.5,"Más de 8.5 córners",.66),("shots",21.5,"Más de 21.5 tiros",.66)]:
        vals = [r[field] for r in rows if r.get(field) is not None]
        if len(vals) >= 6:
            lam2 = statistics.mean(vals) * 2
            p, hr = poisson_over(lam2, line), hit_rate(vals, line)
            if p >= threshold and hr >= .55:
                candidates.append((p, label, f"Ritmo estimado {lam2:.1f}; supera la línea en {hr*100:.0f}%.", "ESPN"))
    for field, line, label in [("total_cards",4.5,"Más de 4.5 tarjetas"),("total_fouls",21.5,"Más de 21.5 faltas")]:
        vals = [r[field] for r in rows if r.get(field) is not None]
        if len(vals) >= 6:
            lam2 = statistics.mean(vals)
            p, hr = poisson_over(lam2, line), hit_rate(vals, line)
            if p >= .66 and hr >= .55:
                candidates.append((p, label, f"Media reciente {lam2:.1f}; supera la línea en {hr*100:.0}%.", "ESPN"))
    for name, rs in [(m["home"], a), (m["away"], b)]:
        for field, line, label in [("corners",4.5,"córners"),("shots",9.5,"tiros"),("sot",3.5,"tiros a puerta")]:
            vals = [r[field] for r in rs if r.get(field) is not None]
            if len(vals) >= 4:
                lam2 = statistics.mean(vals)
                p, hr = poisson_over(lam2, line), hit_rate(vals, line)
                if p >= .68 and hr >= .65:
                    candidates.append((p, f"{name} más de {line} {label}", f"{name}: media {lam2:.1f}; supera la línea en {hr*100:.0f}% de {len(vals)}.", "ESPN"))
    pc = player_candidates(a, m["home"])
    pc.update(player_candidates(b, m["away"]))
    for player, z in pc.items():
        for field, line, label in [("shots",1.5,"tiros"),("sot",.5,"tiros a puerta"),("fouls",1.5,"faltas cometidas")]:
            vals = z[field]
            if len(vals) >= 3:
                hr = hit_rate(vals, line)
                if hr >= .67:
                    candidates.append((hr, f"{player} más de {line} {label}", f"{player}: {sum(v>line for v in vals)}/{len(vals)} partidos por encima.", "ESPN"))
    global _365_match_budget
    sp = statz_player_pick(m["home"], m["away"])
    if sp:
        candidates.append((sp["prob"], sp["market"], sp["why"], sp["source"]))
    if not candidates:
        fallback = []
        if goals:
            fallback.append((hit_rate(goals, 1.5), "Más de 1.5 goles", f"Histórico reciente: {sum(v>1.5 for v in goals)}/{len(goals)} supera 1.5.", "ESPN"))
        for field, line, label in [("corners",8.5,"Más de 8.5 córners"),("shots",21.5,"Más de 21.5 tiros")]:
            vals = [r[field] for r in rows if r.get(field) is not None]
            if vals: fallback.append((hit_rate(vals, line), label, f"Histórico reciente: {sum(v>line for v in vals)}/{len(vals)} supera la línea.", "ESPN"))
        for player, z in pc.items():
            for field, line, label in [("shots",1.5,"tiros"),("sot",.5,"tiros a puerta"),("fouls",1.5,"faltas cometidas")]:
                vals = z[field]
                if len(vals) >= 3:
                    fallback.append((hit_rate(vals, line), f"{player} más de {line} {label}", f"{player}: {sum(v>line for v in vals)}/{len(vals)} partidos por encima.", "ESPN"))
        if fallback:
            p, market, why, source = max(fallback, key=lambda x:x[0])
        else:
            return {"league":m["league"],"match":f'{m["home"]} vs {m["away"]}',"date":m["date"],"time":m["time"],
                    "market":"SIN SEÑAL — datos insuficientes","prob":0,"confidence":"DATOS INSUFICIENTES",
                    "why":"No hubo muestra suficiente para recomendar un mercado sin inventar estadísticas.","source":"Radar","sample":len(rows),
                    "generated_at":datetime.now(timezone.utc).isoformat()}
    else:
        player_candidates_scored = [x for x in candidates if (" tiros" in x[1] or "tiros a puerta" in x[1] or "faltas cometidas" in x[1]) and not x[1].startswith("Más de ")]
        strong_players = [x for x in player_candidates_scored if x[0] >= .67]
        if strong_players:
            p, market, why, source = max(strong_players, key=lambda x:x[0])
        else:
            p, market, why, source = max(candidates, key=lambda x:x[0])
    return {
        "league": m["league"], "match": f'{m["home"]} vs {m["away"]}',
        "date": m["date"], "time": m["time"], "market": market,
        "prob": round(p*100, 1),
        "confidence": "ALTA" if p >= .75 else "MEDIA-ALTA" if p >= .70 else "MEDIA",
        "why": why, "source": source, "sample": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

def classify_all_event(e):
    s = (((e.get("season") or {}).get("slug") or "") + " " + ((e.get("season") or {}).get("displayName") or "")).lower()
    for name in ["Premier League","LaLiga","Serie A","Bundesliga","Ligue 1","Champions League","Europa League","Nations League"]:
        if name.lower() in s:
            return name
    if "friendly" in s or "amistoso" in s:
        return "Amistosos"
    return None

def parse_upcoming_events(data, league):
    out = []
    for e in data.get("events", []):
        c = (e.get("competitions") or [{}])[0]
        teams = c.get("competitors", [])
        if len(teams) != 2: continue
        h = next((x for x in teams if x.get("homeAway") == "home"), teams[0])
        a = next((x for x in teams if x.get("homeAway") == "away"), teams[-1])
        if c.get("status", {}).get("type", {}).get("completed"): continue
        if not h.get("team", {}).get("id") or not a.get("team", {}).get("id"): continue
        try: dt = datetime.fromisoformat(e["date"].replace("Z","+00:00")).astimezone(ZoneInfo("America/Bogota"))
        except Exception: continue
        out.append({"id": e["id"], "home": h["team"]["displayName"], "away": a["team"]["displayName"],
                    "home_id": h["team"]["id"], "away_id": a["team"]["id"], "league": league,
                    "date": dt.strftime("%d/%m/%Y"), "date_iso": dt.date().isoformat(), "time": dt.strftime("%H:%M") + " COL"})
    return out

def upcoming(league, date):
    try:
        d = get_json(f"{BASE}/{LEAGUES[league]}/scoreboard?dates={date.replace('-', '')}")
        rows = parse_upcoming_events(d, league)
        if rows: return rows
        all_d = get_json(f"{BASE}/all/scoreboard?dates={date.replace('-', '')}")
        return [r for e in all_d.get("events", []) if classify_all_event(e) == league
                for r in parse_upcoming_events({"events":[e]}, league)]
    except Exception:
        return []

def main():
    now = datetime.now(timezone.utc)
    matches, seen = [], set()
    for i in range(14):
        day = (now + timedelta(days=i)).date().isoformat()
        for league in LEAGUES:
            for m in upcoming(league, day):
                if m["id"] not in seen:
                    seen.add(m["id"]); matches.append(m)
    signals, reviewed = [], 0
    for m in matches[:160]:
        reviewed += 1
        a, b = team_rows(m["league"], m["home_id"]), team_rows(m["league"], m["away_id"])
        s = signal(m, a, b)
        if s: signals.append(s)
    signals.sort(key=lambda x: x["prob"], reverse=True)
    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "matches": signals, "reviewed": reviewed,
        "markets": 18, "competitions": len(set(m["league"] for m in matches)),
        "source": "ESPN public soccer data + optional 365Scores player enrichment",
        "model": "Recent-match rates + Poisson screen + player hit-rate filter; strongest market per match.",
    }
    os.makedirs("data", exist_ok=True)
    with open("data/radar.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps({"reviewed": reviewed, "signals": len(signals), "competitions": out["competitions"]}))

if __name__ == "__main__":
    main()
