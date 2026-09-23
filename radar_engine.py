import json, math, os, re, statistics, time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

BASE = "https://www.sofascore.com/api/v1"
# Public competition names we want to monitor. We filter SofaScore's global daily schedule by these names.
TARGETS = {
    "Premier League": ["Premier League"],
    "LaLiga": ["LaLiga", "LaLiga EA Sports"],
    "Serie A": ["Serie A"],
    "Bundesliga": ["Bundesliga"],
    "Ligue 1": ["Ligue 1"],
    "Champions League": ["UEFA Champions League", "Champions League"],
    "Europa League": ["UEFA Europa League", "Europa League"],
    "Nations League": ["UEFA Nations League", "Nations League"],
    "Amistosos": ["International Friendly Games", "Friendly International", "Club Friendly Games", "Friendlies"],
}

UA = "EdgeBet-AI/1.0 (+https://github.com/dcasarrubiaquintero-max/daniel)"

def get_json(url, timeout=18):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def norm(s):
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()

def parse_num(v):
    if v is None: return None
    if isinstance(v, (int,float)): return float(v)
    m = re.search(r"-?\d+(?:[\.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None

def poisson_over(lam, threshold):
    if lam <= 0: return 0.0
    k = int(math.floor(threshold))
    cdf = sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k+1))
    return 1-cdf

def poisson_under(lam, threshold):
    # P(X <= floor(threshold))
    k = int(math.floor(threshold))
    if lam <= 0: return 1.0
    return sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k+1))

def stat_items(event_id):
    try:
        data = get_json(f"{BASE}/event/{event_id}/statistics")
    except Exception:
        return {}
    out = {}
    for period in data.get("statistics", []):
        if period.get("period") not in (None, "ALL"):
            continue
        for group in period.get("groups", []):
            for item in group.get("statisticsItems", []):
                name = norm(item.get("name"))
                hv = parse_num(item.get("home"))
                av = parse_num(item.get("away"))
                if hv is not None and av is not None:
                    out[name] = (hv, av)
    return out

def team_last(team_id, limit=5):
    try:
        d = get_json(f"{BASE}/team/{team_id}/events/last/0")
        events = [e for e in d.get("events", []) if e.get("status",{}).get("type") == "finished"]
        return events[:limit]
    except Exception:
        return []

def team_stats(team_id):
    evs = team_last(team_id, 5)
    rows = []
    for e in evs:
        hid = e.get("homeTeam",{}).get("id")
        aid = e.get("awayTeam",{}).get("id")
        if hid != team_id and aid != team_id:
            continue
        hs = e.get("homeScore",{}).get("current", 0) or 0
        aws = e.get("awayScore",{}).get("current", 0) or 0
        st = stat_items(e.get("id"))
        side = "home" if hid == team_id else "away"
        opp = "away" if side == "home" else "home"
        row = {
            "gf": hs if side=="home" else aws,
            "ga": aws if side=="home" else hs,
            "corners": st.get("corner kicks",(None,None))[0 if side=="home" else 1],
            "opp_corners": st.get("corner kicks",(None,None))[1 if side=="home" else 0],
            "shots": st.get("total shots",(None,None))[0 if side=="home" else 1],
            "sot": st.get("shots on target",(None,None))[0 if side=="home" else 1],
            "opp_shots": st.get("total shots",(None,None))[1 if side=="home" else 0],
            "cards": st.get("yellow cards",(None,None))[0 if side=="home" else 1],
            "fouls": st.get("fouls",(None,None))[0 if side=="home" else 1],
        }
        rows.append(row)
    return rows

def mean(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return statistics.mean(vals) if vals else None

def signal(match, league, arows, brows):
    # Conservative model: rates are calculated from recent completed matches.
    # We publish only one strongest market when sample coverage is adequate.
    markets = []
    allrows = arows + brows
    n = len(allrows)
    if n < 6:
        return None

    g = [x["gf"] + x["ga"] for x in allrows]
    corners = [x["corners"] + x["opp_corners"] for x in allrows if x["corners"] is not None and x["opp_corners"] is not None]
    shots = [x["shots"] + x["opp_shots"] for x in allrows if x["shots"] is not None and x["opp_shots"] is not None]
    cards = [x["cards"] for x in allrows if x["cards"] is not None]

    if len(g) >= 6:
        lam = statistics.mean(g)
        p = poisson_over(lam, 2.5)
        if p >= 0.66:
            markets.append((p, "Más de 2.5 goles", f"Promedio combinado reciente: {lam:.2f} goles por partido."))

    if len(corners) >= 6:
        lam = statistics.mean(corners)
        p = poisson_over(lam, 8.5)
        if p >= 0.66:
            markets.append((p, "Más de 8.5 córners", f"Promedio reciente: {lam:.2f} córners por partido."))

    if len(shots) >= 6:
        lam = statistics.mean(shots)
        p = poisson_over(lam, 21.5)
        if p >= 0.66:
            markets.append((p, "Más de 21.5 tiros", f"Promedio reciente: {lam:.1f} tiros totales por partido."))

    if len(cards) >= 6:
        lam = statistics.mean(cards)
        p = poisson_over(lam, 3.5)
        if p >= 0.66:
            markets.append((p, "Más de 3.5 tarjetas", f"Promedio reciente: {lam:.2f} tarjetas por equipo/partido observado."))

    # Team-specific corner and shot signals
    for name, rows in [(match["home"], arows), (match["away"], brows)]:
        cs = [r["corners"] for r in rows if r.get("corners") is not None]
        ss = [r["shots"] for r in rows if r.get("shots") is not None]
        if len(cs) >= 4:
            lam = statistics.mean(cs)
            p = poisson_over(lam, 4.5)
            if p >= 0.68:
                markets.append((p, f"{name} más de 4.5 córners", f"{name} promedia {lam:.2f} córners en sus últimos {len(cs)} partidos medidos."))
        if len(ss) >= 4:
            lam = statistics.mean(ss)
            p = poisson_over(lam, 9.5)
            if p >= 0.68:
                markets.append((p, f"{name} más de 9.5 tiros", f"{name} promedia {lam:.1f} tiros en sus últimos {len(ss)} partidos medidos."))

    if not markets:
        return None
    markets.sort(key=lambda x:x[0], reverse=True)
    p, market, why = markets[0]
    conf = "ALTA" if p >= .75 and n >= 8 else "MEDIA-ALTA" if p >= .70 else "MEDIA"
    return {
        "league": league,
        "match": f'{match["home"]} vs {match["away"]}',
        "date": match["date"],
        "time": match["time"],
        "market": market,
        "prob": round(p*100,1),
        "confidence": conf,
        "why": why,
        "sample": n,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

def classify(tname):
    nt = norm(tname)
    for league, aliases in TARGETS.items():
        if any(norm(a) == nt or norm(a) in nt or nt in norm(a) for a in aliases):
            return league
    return None

def main():
    now = datetime.now(timezone.utc)
    days = [(now + timedelta(days=i)).date().isoformat() for i in range(0, 8)]
    matches = []
    seen = set()
    for day in days:
        try:
            data = get_json(f"{BASE}/sport/football/scheduled-events/{day}/inverse")
        except Exception:
            continue
        for e in data.get("events", []):
            if e.get("status",{}).get("type") != "notstarted":
                continue
            tournament = e.get("tournament",{})\n            unique = e.get("tournament",{}).get("uniqueTournament",{})\n            league = classify(unique.get("name","")) or classify(tournament.get("name",""))
            if not league:
                continue
            hid = e.get("homeTeam",{}).get("id")
            aid = e.get("awayTeam",{}).get("id")
            if not hid or not aid or e.get("id") in seen:
                continue
            seen.add(e["id"])
            ts = e.get("startTimestamp")
            dt = datetime.fromtimestamp(ts, timezone.utc) if ts else now
            matches.append({"id":e["id"],"home":e["homeTeam"]["name"],"away":e["awayTeam"]["name"],"home_id":hid,"away_id":aid,"league":league,"date":dt.date().isoformat(),"time":dt.strftime("%H:%M UTC")})

    signals = []
    reviewed = 0
    for m in matches[:40]:
        reviewed += 1
        a = team_stats(m["home_id"])
        b = team_stats(m["away_id"])
        s = signal(m, m["league"], a, b)
        if s:
            signals.append(s)

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "matches": signals,
        "reviewed": reviewed,
        "markets": 6,
        "competitions": len(set(m["league"] for m in matches)),
        "source": "SofaScore public football endpoints; fixtures/statistics cross-checked by EdgeBet model",
        "model": "Recent-rate Poisson screen; signals require minimum sample coverage and probability threshold."
    }
    os.makedirs("data", exist_ok=True)
    with open("data/radar.json","w",encoding="utf-8") as f:
        json.dump(out,f,ensure_ascii=False,indent=2)
    print(json.dumps({"reviewed":reviewed,"signals":len(signals),"competitions":out["competitions"]}))

if __name__ == "__main__":
    main()
