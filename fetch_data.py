#!/usr/bin/env python3
"""抓取 CS（S 级赛事）和 KPL（全部）赛程，写入 data.json。

数据源：Liquipedia（遵守其 API 规则：带 User-Agent、gzip、parse 请求间隔 30 秒）。
NBA 数据由网页在浏览器里实时从 ESPN 获取，不在这里抓。

用法：python3 fetch_data.py
"""
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

UA = "PersonalEsportsCalendar/1.0 (single-user personal calendar; github actions)"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data.json")
PARSE_DELAY = 31  # Liquipedia: action=parse 最多每 30 秒一次

_last_call = 0.0


def api_parse(wiki, *, page=None, text=None):
    global _last_call
    wait = PARSE_DELAY - (time.time() - _last_call)
    if _last_call and wait > 0:
        time.sleep(wait)
    params = {"action": "parse", "format": "json", "prop": "text"}
    if page:
        params["page"] = page
    else:
        params["text"] = text
        params["contentmodel"] = "wikitext"
    url = f"https://liquipedia.net/{wiki}/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    _last_call = time.time()
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(f"{wiki} {page or text[:60]}: {data['error']}")
    return data["parse"]["text"]["*"]


def ticker(wiki, kind, tier=None, limit=500):
    t = f"{{{{#invoke:Lua|invoke|module=MatchTicker/Custom|fn=mainPage|type={kind}|limit={limit}"
    if tier:
        t += f"|filterbuttons-liquipediatier={tier}"
    t += "}}"
    return api_parse(wiki, text=t)


def strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).replace("\xa0", " ").strip()


def parse_matches(h, wiki, game):
    out = []
    blocks = h.split('<div class="match-info">')[1:]
    for b in blocks:
        ts = re.search(r'data-timestamp="(\d+)"', b)
        if not ts:
            continue
        names = re.findall(r'<span class="name"[^>]*>(.*?)</span>', b, re.S)
        teams = [strip(n) or "TBD" for n in names[:2]]
        while len(teams) < 2:
            teams.append("TBD")
        upper = re.search(r'scoreholder-upper"[^>]*>(.*?)<span class="match-info-header-scoreholder-lower', b, re.S)
        lower = re.search(r'scoreholder-lower"[^>]*>(.*?)</span>', b, re.S)
        score = strip(upper.group(1)) if upper else ""
        if score.lower() == "vs":
            score = ""
        bo = strip(lower.group(1)).strip("()") if lower else ""
        tour = re.search(r'match-info-tournament-name"><a href="([^"]*)"[^>]*>(.*?)</a>', b, re.S)
        tname = strip(tour.group(2)) if tour else ""
        link = ("https://liquipedia.net" + html.unescape(tour.group(1))) if tour else ""
        vods = []
        area = re.search(r'match-info-vods-area(.*)', b, re.S)
        if area:
            for href, label in re.findall(r'<a [^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', area.group(1), re.S):
                lab = strip(label)
                g = re.match(r"Game\s*(\d+)", lab, re.I)
                lab = f"第{g.group(1)}局" if g else (lab or "回放")
                vods.append({"l": lab, "u": html.unescape(href)})
        out.append({
            "game": game,
            "vods": vods,
            "ts": int(ts.group(1)),
            "t1": teams[0],
            "t2": teams[1],
            "score": score,
            "bo": bo,
            "event": tname,
            "link": link,
        })
    return out


MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_range(s):
    """'Nov 22 – Dec 12, 2027' / 'Nov 08–14, 2027' / 'Dec 05, 2026' -> (start, end) ISO."""
    s = s.replace("–", "-").replace("—", "-").replace(" ", "")
    m = re.match(r"([A-Za-z]{3})(\d{1,2})(?:-(?:([A-Za-z]{3}))?(\d{1,2}))?,(\d{4})$", s)
    if not m:
        return None
    m1, d1, m2, d2, y = m.groups()
    y = int(y)
    sm, em = MONTHS.get(m1), MONTHS.get(m2 or m1)
    if not sm or not em:
        return None
    sy = y - 1 if em < sm else y
    start = date(sy, sm, int(d1))
    end = date(y, em, int(d2 or d1))
    return start.isoformat(), end.isoformat()


def parse_tournament_table(h, game, name_filter=None, min_year=None):
    out = []
    rows = re.findall(r'<tr class="table2&#95;&#95;row--body[^"]*">(.*?)</tr>', h, re.S)
    for r in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        cells = [strip(t) for t in tds]
        rng = None
        name = ""
        for i, c in enumerate(cells):
            rng = parse_range(c)
            if rng:
                name = next((x for x in reversed(cells[:i]) if x), "")
                break
        if not rng or not name:
            continue
        if name_filter and not name_filter(name):
            continue
        if min_year and int(rng[1][:4]) < min_year:
            continue
        href = re.search(r'class="column&#95;&#95;tournament".*?<a href="([^"]*)"', r, re.S) \
            or re.search(r'<a href="(/[^"]*)" title="' + re.escape(html.escape(name, quote=True)), r)
        link = ("https://liquipedia.net" + html.unescape(href.group(1))) if href else ""
        out.append({"game": game, "name": name, "start": rng[0], "end": rng[1], "link": link})
    # 去重
    seen, uniq = set(), []
    for t in out:
        k = (t["name"], t["start"])
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


def is_kpl(name):
    n = name.lower()
    return "kpl" in n or "king pro league" in n


def main():
    year_min = datetime.now(timezone.utc).year - 1
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}

    matches, tournaments, errors = [], [], []

    def step(label, fn):
        try:
            res = fn()
            print(f"[ok] {label}: {len(res)}", file=sys.stderr)
            return res
        except Exception as e:  # 单个来源失败不影响其他
            print(f"[fail] {label}: {e}", file=sys.stderr)
            errors.append(f"{label}: {e}")
            return []

    matches += step("CS S级 即将进行", lambda: parse_matches(ticker("counterstrike", "upcoming", tier=1), "counterstrike", "cs"))
    matches += step("CS S级 已结束", lambda: parse_matches(ticker("counterstrike", "recent", tier=1), "counterstrike", "cs"))
    tournaments += step("CS S级赛事列表", lambda: parse_tournament_table(
        api_parse("counterstrike", page="S-Tier_Tournaments"), "cs", min_year=year_min))
    matches += step("KPL 即将进行", lambda: [m for m in parse_matches(ticker("honorofkings", "upcoming"), "honorofkings", "kpl") if is_kpl(m["event"])])
    matches += step("KPL 已结束", lambda: [m for m in parse_matches(ticker("honorofkings", "recent"), "honorofkings", "kpl") if is_kpl(m["event"])])
    tournaments += step("KPL 赛事列表", lambda: parse_tournament_table(
        api_parse("honorofkings", page="King_Pro_League"), "kpl", name_filter=is_kpl, min_year=year_min))

    # 与旧数据合并：保留历史比赛（新数据覆盖同一场次）
    def key(m):
        return f'{m["game"]}|{m["ts"]}|{m["event"]}|{m["t1"]}|{m["t2"]}'

    merged = {}
    cutoff = time.time() - 400 * 86400
    now = time.time()
    fresh_games = {m["game"] for m in matches}
    for m in old.get("matches", []):
        if m["ts"] < cutoff:
            continue
        # 未来的旧比赛若本次已成功抓到该项目，则以新数据为准（可能已改期）
        if m["ts"] > now and m["game"] in fresh_games:
            continue
        merged[key(m)] = m
    # 同一时间同一赛事的 TBD 旧条目，被确定了队伍的新条目替换
    for m in matches:
        merged[key(m)] = m
        if "TBD" not in (m["t1"], m["t2"]):
            for side in ((m["t1"], "TBD"), ("TBD", m["t2"]), ("TBD", "TBD")):
                merged.pop(f'{m["game"]}|{m["ts"]}|{m["event"]}|{side[0]}|{side[1]}', None)

    if not tournaments:
        tournaments = old.get("tournaments", [])

    data = {
        "updated": int(time.time()),
        "errors": errors,
        "matches": sorted(merged.values(), key=lambda m: m["ts"]),
        "tournaments": sorted(tournaments, key=lambda t: t["start"]),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {OUT}: {len(data['matches'])} matches, {len(data['tournaments'])} tournaments", file=sys.stderr)
    if len(errors) == 6:
        sys.exit(1)


if __name__ == "__main__":
    main()
