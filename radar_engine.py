import json, math, os, re, statistics, unicodedata
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

try:
    from scores365_client import find_match, fixture_rows, normalize_player_stats, lineups, norm
except Exception:
    find_match = fixture_rows = normalize_player_stats = lineups = None

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
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
    match = f'{m["home"]} vs {m["away"]}'
    base = {"league":m["league"],"match":match,"date":m["date"],"time":m["time"],
            "source":"Radar","sample":len(a)+len(b),"generated_at":datetime.now(timezone.utc).isoformat()}
    if len(a) < 5 or len(b) < 5:
        return {**base,"market":"SIN SEÑAL","prob":0,"confidence":"SIN SEÑAL","why":"Muestra insuficiente."}
    candidates=[]
    goals=[r["gf"]+r["ga"] for r in a+b]
    for line,minimum in [(1.5,.72),(2.5,.58)]:
        hr=hit_rate(goals,line); lam=statistics.mean(goals)
        p=.55*hr+.45*poisson_over(lam,line)
        if hr>=minimum and p>=(.70 if line==1.5 else .60):
            candidates.append((p,f"Más de {line} goles",f"Se superó {line} en {sum(v>line for v in goals)}/{len(goals)} partidos; media {lam:.2f}."))
    bh=sum(r["gf"]>0 and r["ga"]>0 for r in a)/len(a)
    ba=sum(r["gf"]>0 and r["ga"]>0 for r in b)/len(b)
    bp=(bh+ba)/2
    if bp>=.65:
        candidates.append((bp,"Ambos equipos marcan",f"BTTS en {bh*100:.0f}% del local y {ba*100:.0f}% del visitante."))
    ca=[r["corners"] for r in a+b if r.get("corners") is not None]
    if len(ca)>=8:
        for line,minimum in [(7.5,.70),(8.5,.66),(9.5,.62),(10.5,.58)]:
            hr=hit_rate(ca,line); lam=statistics.mean(ca); p=.55*hr+.45*poisson_over(lam,line)
            if hr>=minimum and p>=.63:
                candidates.append((p,f"Más de {line} córners",f"Se superó {line} córners en {sum(v>line for v in ca)}/{len(ca)}; media {lam:.1f}."))
    for team,rs in [(m["home"],a),(m["away"],b)]:
        vals=[r["corners"] for r in rs if r.get("corners") is not None]
        if len(vals)>=5:
            for line,minimum in [(3.5,.72),(4.5,.66),(5.5,.60)]:
                hr=hit_rate(vals,line); lam=statistics.mean(vals); p=.55*hr+.45*poisson_over(lam,line)
                if hr>=minimum and p>=.63:
                    candidates.append((p,f"{team} más de {line} córners",f"{team}: supera {line} en {sum(v>line for v in vals)}/{len(vals)}; media {lam:.1f}."))
    if not candidates:
        return {**base,"market":"SIN SEÑAL","prob":0,"confidence":"SIN SEÑAL","why":"No hay evidencia suficiente en goles, BTTS o córners."}
    p,market,why=max(candidates,key=lambda x:x[0])
    return {**base,"market":market,"prob":round(p*100,1),"confidence":"ALTA" if p>=.75 else "MEDIA-ALTA" if p>=.68 else "MEDIA","why":why}

