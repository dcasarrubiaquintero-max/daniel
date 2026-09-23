import json, math, os, re, statistics
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES = {
    "Premier League":"eng.1",
    "LaLiga":"esp.1",
    "Serie A":"ita.1",
    "Bundesliga":"ger.1",
    "Ligue 1":"fra.1",
    "Champions League":"uefa.champions",
    "Europa League":"uefa.europa",
    "Nations League":"uefa.nations",
    "Amistosos":"fifa.friendly",
}
UA="EdgeBet-AI/2.0"

def get_json(url):
    req=Request(url,headers={"User-Agent":UA,"Accept":"application/json"})
    with urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def parse_num(v):
    if v is None:return None
    if isinstance(v,(int,float)):return float(v)
    m=re.search(r"-?\d+(?:[\.,]\d+)?",str(v))
    return float(m.group(0).replace(",",".")) if m else None

def poisson_over(lam,threshold):
    if lam<=0:return 0.0
    k=int(math.floor(threshold))
    return 1-sum(math.exp(-lam)*lam**i/math.factorial(i) for i in range(k+1))

def stat_value(stats,name):
    for s in stats or []:
        label=(s.get("name") or s.get("displayName") or "").lower()
        if name in label:
            return parse_num(s.get("value"))
    return None

def summary_stats(event_id):
    try:d=get_json(f"{BASE}/eng.1/summary?event={event_id}")
    except Exception:return {}
    out={}
    box=d.get("boxscore",{}).get("teams",[])
    for b in box:
        team=b.get("team",{}).get("displayName")
        vals={}
        for s in b.get("statistics",[]) or []:
            label=(s.get("name") or s.get("displayName") or "").lower()
            val=parse_num(s.get("displayValue",s.get("value")))
            if val is not None: vals[label]=val
        if team:out[team]=vals
    return out

def summary(event_id,league):
    try:d=get_json(f"{BASE}/{LEAGUES[league]}/summary?event={event_id}")
    except Exception:return None
    comps=d.get("header",{}).get("competitions",[])
    if not comps:return None
    c=comps[0]
    teams=c.get("competitors",[])
    if len(teams)!=2:return None
    home=next((x for x in teams if x.get("homeAway")=="home"),teams[0])
    away=next((x for x in teams if x.get("homeAway")=="away"),teams[-1])
    hs=parse_num(home.get("score")); aws=parse_num(away.get("score"))
    if hs is None or aws is None:return None
    return {"home":home.get("team",{}).get("displayName"),"away":away.get("team",{}).get("displayName"),
            "gf":hs,"ga":aws,"stats":d.get("boxscore",{}).get("teams",[])}

def extract_team_row(s,team_name):
    for b in s.get("stats",[]) or []:
        if b.get("team",{}).get("displayName")==team_name:
            vals={}
            for x in b.get("statistics",[]) or []:
                label=(x.get("name") or x.get("displayName") or "").lower()
                val=parse_num(x.get("displayValue",x.get("value")))
                if val is not None: vals[label]=val
            def pick(*terms):
                for k,v in vals.items():
                    if any(t in k for t in terms):return v
                return None
            return {"gf":s["gf"],"ga":s["ga"],
                    "corners":pick("corner"),
                    "shots":pick("total shots","shots"),
                    "sot":pick("shots on target"),
                    "cards":pick("yellow card"),
                    "fouls":pick("foul")}
    return None

def past_team_events(league,team_id,limit=5):
    # ESPN team schedules expose recent completed events without needing an API key.
    try:d=get_json(f"{BASE}/{LEAGUES[league]}/teams/{team_id}/schedule")
    except Exception:return []
    ev=[]
    for e in d.get("events",[]):
        st=e.get("competitions",[{}])[0].get("status",{}).get("type",{}).get("completed")
        if st: ev.append(e)
    ev.sort(key=lambda x:x.get("date",""),reverse=True)
    return ev[:limit]

def row_for_event(e,league,team_id):
    s=summary(e.get("id"),league)
    if not s:return None
    home_id=(e.get("competitions",[{}])[0].get("competitors",[{}])[0].get("team",{}).get("id"))
    side_home=home_id==str(team_id)
    team=s["home"] if side_home else s["away"]
    opp=s["away"] if side_home else s["home"]
    row=extract_team_row(s,team)
    if not row:return None
    row["opp"]=opp
    return row

def team_rows(league,team_id):
    rows=[]
    for e in past_team_events(league,team_id,5):
        r=row_for_event(e,league,team_id)
        if r:rows.append(r)
    return rows

def signal(m,a,b):
    rows=a+b
    if len(rows)<6:return None
    candidates=[]
    goals=[r["gf"]+r["ga"] for r in rows]
    lam=statistics.mean(goals);p=poisson_over(lam,2.5)
    if p>=.66:candidates.append((p,"Más de 2.5 goles",f"Media reciente: {lam:.2f} goles."))
    corners=[r["corners"] for r in rows if r["corners"] is not None]
    if len(corners)>=6:
        lam=statistics.mean(corners)*2
        p=poisson_over(lam,8.5)
        if p>=.66:candidates.append((p,"Más de 8.5 córners",f"Ritmo reciente estimado: {lam:.1f} córners."))
    shots=[r["shots"] for r in rows if r["shots"] is not None]
    if len(shots)>=6:
        lam=statistics.mean(shots)*2
        p=poisson_over(lam,21.5)
        if p>=.66:candidates.append((p,"Más de 21.5 tiros",f"Ritmo reciente estimado: {lam:.1f} tiros."))
    cards=[r["cards"] for r in rows if r["cards"] is not None]
    if len(cards)>=6:
        lam=statistics.mean(cards)
        p=poisson_over(lam,3.5)
        if p>=.66:candidates.append((p,"Más de 3.5 tarjetas",f"Media reciente: {lam:.2f} tarjetas por equipo observado."))
    for name,rs in [(m["home"],a),(m["away"],b)]:
        for key,line,label in [("corners",4.5,"córners"),("shots",9.5,"tiros")]:
            vals=[r[key] for r in rs if r.get(key) is not None]
            if len(vals)>=4:
                lam=statistics.mean(vals);p=poisson_over(lam,line)
                if p>=.68:candidates.append((p,f"{name} más de {line} {label}",f"{name} promedia {lam:.1f} en sus últimos {len(vals)} partidos medidos."))
    if not candidates:return None
    p,market,why=max(candidates,key=lambda x:x[0])
    return {"league":m["league"],"match":f'{m["home"]} vs {m["away"]}',"date":m["date"],"time":m["time"],
            "market":market,"prob":round(p*100,1),"confidence":"ALTA" if p>=.75 else "MEDIA-ALTA" if p>=.70 else "MEDIA",
            "why":why,"sample":len(rows),"generated_at":datetime.now(timezone.utc).isoformat()}

def upcoming(league,date):
    try:d=get_json(f"{BASE}/{LEAGUES[league]}/scoreboard?dates={date.replace('-','')}")
    except Exception:return []
    out=[]
    for e in d.get("events",[]):
        c=(e.get("competitions") or [{}])[0]
        teams=c.get("competitors",[])
        if len(teams)!=2:continue
        h=next((x for x in teams if x.get("homeAway")=="home"),teams[0])
        a=next((x for x in teams if x.get("homeAway")=="away"),teams[-1])
        if c.get("status",{}).get("type",{}).get("completed"):continue
        if not h.get("team",{}).get("id") or not a.get("team",{}).get("id"):continue
        dt=datetime.fromisoformat(e["date"].replace("Z","+00:00"))
        out.append({"id":e["id"],"home":h["team"]["displayName"],"away":a["team"]["displayName"],
                    "home_id":h["team"]["id"],"away_id":a["team"]["id"],"league":league,
                    "date":dt.date().isoformat(),"time":dt.strftime("%H:%M UTC")})
    return out

def main():
    now=datetime.now(timezone.utc)
    matches=[];seen=set()
    for i in range(8):
        day=(now+timedelta(days=i)).date().isoformat()
        for league in LEAGUES:
            for m in upcoming(league,day):
                if m["id"] not in seen:seen.add(m["id"]);matches.append(m)
    signals=[];reviewed=0
    for m in matches[:40]:
        reviewed+=1
        a=team_rows(m["league"],m["home_id"]);b=team_rows(m["league"],m["away_id"])
        s=signal(m,a,b)
        if s:signals.append(s)
    out={"updated_at":datetime.now(timezone.utc).isoformat(),"matches":signals,"reviewed":reviewed,
         "markets":6,"competitions":len(set(m["league"] for m in matches)),
         "source":"ESPN public soccer scoreboard/summaries; EdgeBet statistical model",
         "model":"Recent-match rate screen with minimum coverage and probability thresholds."}
    os.makedirs("data",exist_ok=True)
    with open("data/radar.json","w",encoding="utf-8") as f:json.dump(out,f,ensure_ascii=False,indent=2)
    print(json.dumps({"reviewed":reviewed,"signals":len(signals),"competitions":out["competitions"]}))

if __name__=="__main__":main()
