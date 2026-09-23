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

def extract_player_rows(summary_data, team_name):
    """Normalize ESPN player boxscore rows when labels are exposed."""
    out=[]
    players=summary_data.get("boxscore",{}).get("players",[]) or []
    def walk(node):
        if isinstance(node,dict):
            athlete=node.get("athlete") or {}
            name=athlete.get("displayName")
            blocks=node.get("statistics")
            if name and isinstance(blocks,list):
                vals={}
                for block in blocks:
                    if not isinstance(block,dict): continue
                    labels=block.get("labels") or block.get("names") or []
                    raw=block.get("stats") or block.get("statistics") or []
                    if isinstance(raw,list) and labels and len(raw)==len(labels):
                        for label,val in zip(labels,raw):
                            num=parse_num(val)
                            if num is not None: vals[str(label).lower()]=num
                if vals: out.append({"name":name,"stats":vals,"team":team_name})
            for v in node.values(): walk(v)
        elif isinstance(node,list):
            for v in node: walk(v)
    walk(players)
    return out

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
            "gf":hs,"ga":aws,"stats":d.get("boxscore",{}).get("teams",[]),
            "players":d.get("boxscore",{}).get("players",[])}

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
    # Match-level totals: aggregate both teams so cards/fouls are not double-counted as one side.
    team_blocks=s.get("stats",[]) or []
    all_cards=[];all_fouls=[]
    for b in team_blocks:
        vals={}
        for x in b.get("statistics",[]) or []:
            label=(x.get("name") or x.get("displayName") or "").lower()
            val=parse_num(x.get("displayValue",x.get("value")))
            if val is not None: vals[label]=val
        for k,v in vals.items():
            if "yellow card" in k: all_cards.append(v); break
        for k,v in vals.items():
            if "foul" in k: all_fouls.append(v); break
    row["total_cards"]=sum(all_cards) if len(all_cards)==2 else None
    row["total_fouls"]=sum(all_fouls) if len(all_fouls)==2 else None
    row["opp"]=opp
    row["venue"]="home" if side_home else "away"
    row["player_rows"]=extract_player_rows({"boxscore":{"players":s.get("players",[])}},team)
    return row

def team_rows(league,team_id):
    rows=[]
    for e in past_team_events(league,team_id,5):
        r=row_for_event(e,league,team_id)
        if r:rows.append(r)
    return rows
def player_candidates(rows, team_name):
    candidates=[]
    for r in rows:
        for p in r.get("player_rows",[]):
            if p.get("team")!=team_name: continue
            st=p.get("stats",{})
            shots=next((v for k,v in st.items() if k in ("shots","total shots","totalshots")),None)
            sot=next((v for k,v in st.items() if k in ("shots on target","shotsontarget","sot")),None)
            fouls=next((v for k,v in st.items() if k in ("fouls","fouls committed","foulscommitted")),None)
            if shots is not None or sot is not None or fouls is not None:
                candidates.append((p.get("name"),shots,sot,fouls))
    return candidates

def signal(m,a,b):
    rows=a+b
    if len(rows)<6:return None
    candidates=[]
    # Goal markets: totals and BTTS from each recent match.
    goals=[r["gf"]+r["ga"] for r in rows]
    btts=[1 if r["gf"]>0 and r["ga"]>0 else 0 for r in rows]
    btts_rate=sum(btts)/len(btts) if btts else 0
    def hit_rate(vals,line):
        return sum(1 for x in vals if x>line)/len(vals) if vals else 0
    lam=statistics.mean(goals);p=poisson_over(lam,2.5);hr=hit_rate(goals,2.5)
    if p>=.66 and hr>=.55:candidates.append((p,"Más de 2.5 goles",f"Media reciente: {lam:.2f}; se superó en {hr*100:.0f}% de la muestra."))
    p15=poisson_over(lam,1.5);hr15=hit_rate(goals,1.5)
    if p15>=.78 and hr15>=.70:candidates.append((p15,"Más de 1.5 goles",f"Media reciente: {lam:.2f}; se superó en {hr15*100:.0f}% de la muestra."))
    p35=poisson_over(lam,3.5);hr35=hit_rate(goals,3.5)
    if p35>=.60 and hr35>=.45:candidates.append((p35,"Más de 3.5 goles",f"Media reciente: {lam:.2f}; se superó en {hr35*100:.0f}% de la muestra."))
    if btts_rate>=.62:candidates.append((btts_rate,"Ambos equipos marcan",f"BTTS en {btts_rate*100:.0f}% de los últimos {len(rows)} partidos medidos."))
    corners=[r["corners"] for r in rows if r["corners"] is not None]
    if len(corners)>=6:
        lam=statistics.mean(corners)*2
        p=poisson_over(lam,8.5)
        hr=hit_rate(corners,8.5)
        if p>=.66 and hr>=.55:candidates.append((p,"Más de 8.5 córners",f"Ritmo reciente estimado: {lam:.1f}; se superó en {hr*100:.0f}% de la muestra."))
    shots=[r["shots"] for r in rows if r["shots"] is not None]
    if len(shots)>=6:
        lam=statistics.mean(shots)*2
        p=poisson_over(lam,21.5)
        hr=hit_rate(shots,21.5)
        if p>=.66 and hr>=.55:candidates.append((p,"Más de 21.5 tiros",f"Ritmo reciente estimado: {lam:.1f}; se superó en {hr*100:.0f}% de la muestra."))
    total_cards=[r["total_cards"] for r in rows if r.get("total_cards") is not None]
    if len(total_cards)>=6:
        lam=statistics.mean(total_cards);p=poisson_over(lam,4.5);hr=hit_rate(total_cards,4.5)
        if p>=.66 and hr>=.55:candidates.append((p,"Más de 4.5 tarjetas",f"Media reciente: {lam:.2f}; se superó en {hr*100:.0f}% de la muestra."))
    total_fouls=[r["total_fouls"] for r in rows if r.get("total_fouls") is not None]
    if len(total_fouls)>=6:
        lam=statistics.mean(total_fouls);p=poisson_over(lam,21.5);hr=hit_rate(total_fouls,21.5)
        if p>=.66 and hr>=.55:candidates.append((p,"Más de 21.5 faltas",f"Media reciente: {lam:.1f}; se superó en {hr*100:.0f}% de la muestra."))
    cards=[r["cards"] for r in rows if r["cards"] is not None]
    if len(cards)>=6:
        lam=statistics.mean(cards)
        p=poisson_over(lam,3.5);hr=hit_rate(cards,3.5)
        if p>=.66 and hr>=.55:candidates.append((p,"Más de 3.5 tarjetas",f"Media reciente: {lam:.2f} por equipo; se superó en {hr*100:.0f}% de la muestra."))
    for name,rs in [(m["home"],a),(m["away"],b)]:
        for key,line,label in [("corners",4.5,"córners"),("shots",9.5,"tiros")]:
            vals=[r[key] for r in rs if r.get(key) is not None]
            if len(vals)>=4:
                lam=statistics.mean(vals);p=poisson_over(lam,line);hr=hit_rate(vals,line)
                if p>=.68 and hr>=.65:candidates.append((p,f"{name} más de {line} {label}",f"{name} promedia {lam:.1f} en sus últimos {len(vals)} partidos; supera la línea en {hr*100:.0f}%."))
        vals_sot=[r["sot"] for r in rs if r.get("sot") is not None]
        if len(vals_sot)>=4:
            lam=statistics.mean(vals_sot);line=3.5;p=poisson_over(lam,line);hr=hit_rate(vals_sot,line)
            if p>=.68 and hr>=.65:candidates.append((p,f"{name} más de {line} tiros a puerta",f"{name} promedia {lam:.1f} tiros a puerta en {len(vals_sot)} partidos; supera la línea en {hr*100:.0f}%."))
    # Player markets: only exact labeled statistics; unresolved fields are skipped.
    for name,rs in [(m["home"],a),(m["away"],b)]:
        pc=player_candidates(rs,name)
        by={}
        for pn,shots,sot,fouls in pc:
            z=by.setdefault(pn,{"shots":[],"sot":[],"fouls":[]})
            if shots is not None:z["shots"].append(shots)
            if sot is not None:z["sot"].append(sot)
            if fouls is not None:z["fouls"].append(fouls)
        for pn,z in by.items():
            for key,line,label in [("shots",1.5,"tiros"),("sot",0.5,"tiros a puerta"),("fouls",1.5,"faltas cometidas")]:
                vals=z[key]
                if len(vals)>=3:
                    hr=hit_rate(vals,line);avg=statistics.mean(vals)
                    if hr>=.67:
                        candidates.append((hr,f"{pn} más de {line} {label}",f"{pn}: promedio {avg:.2f}; supera la línea en {hr*100:.0f}% de {len(vals)} partidos medidos."))
    # Player markets are only emitted when ESPN exposes a consistent numeric player feed.
    # We intentionally do not guess stat-column positions; unresolved player stats are skipped.
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
    for i in range(10):
        day=(now+timedelta(days=i)).date().isoformat()
        for league in LEAGUES:
            for m in upcoming(league,day):
                if m["id"] not in seen:seen.add(m["id"]);matches.append(m)
    signals=[];reviewed=0
    for m in matches[:120]:
        reviewed+=1
        a=team_rows(m["league"],m["home_id"]);b=team_rows(m["league"],m["away_id"])
        s=signal(m,a,b)
        if s:signals.append(s)
    signals.sort(key=lambda x: x["prob"], reverse=True)
    out={"updated_at":datetime.now(timezone.utc).isoformat(),"matches":signals,"reviewed":reviewed,
         "markets":15,"competitions":len(set(m["league"] for m in matches)),
         "source":"ESPN public soccer scoreboard/summaries; EdgeBet statistical model",
         "Recent-match rate + hit-rate screen; match totals aggregate both teams; strongest market only."}
    os.makedirs("data",exist_ok=True)
    with open("data/radar.json","w",encoding="utf-8") as f:json.dump(out,f,ensure_ascii=False,indent=2)
    print(json.dumps({"reviewed":reviewed,"signals":len(signals),"competitions":out["competitions"]}))

if __name__=="__main__":main()
