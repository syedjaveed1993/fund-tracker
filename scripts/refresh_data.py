"""
India Fund Tracker - Cloud Data Refresh
Runs on GitHub Actions daily. Re-fetches live NAV data from mfapi.in
for every tracked fund, discovers new funds, recomputes all metrics,
and writes fund_data.json next to index.html.

Run locally to test:  python scripts/refresh_data.py
"""

import json, math, os, sys, time, urllib.request, urllib.error
from collections import defaultdict, Counter
from datetime import datetime, timedelta

REPO_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(REPO_DIR, "fund_data.json")
BASE      = "https://api.mfapi.in/mf"
RF        = 7.0          # India risk-free rate %
PAUSE     = 0.12         # seconds between API calls (be polite)

TARGET_CATEGORIES = [
    "small cap", "mid cap", "large cap", "flexi cap",
    "large & mid cap", "large and mid cap",
    "index funds", "index fund", "other index",
    "sectoral", "thematic", "sector",
]


# -- HTTP ---------------------------------------------------------------------
def fetch_json(url, retries=3, timeout=45, verbose=False):
    """Fetch JSON. When verbose, report why it failed instead of
    swallowing the exception - otherwise failures are undiagnosable."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/122.0 Safari/537.36"),
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = "HTTP %s %s" % (e.code, e.reason)
            # Rate limiting - back off hard rather than hammering
            if e.code in (429, 503):
                time.sleep(10 + attempt * 20)
                continue
        except urllib.error.URLError as e:
            last = "URLError: %s" % e.reason
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < retries - 1:
            time.sleep(3 + attempt * 5)
    if verbose:
        print("    fetch failed: %s  <- %s" % (last, url))
    return None


# -- Math ---------------------------------------------------------------------
def parse_date(s):
    d, m, y = s.split("-")
    return datetime(int(y), int(m), int(d))


def calc_return_interp(hist, days):
    """Return % over `days`, interpolating between the two NAV points
    that straddle the target date."""
    if not hist or len(hist) < 2:
        return None
    try:
        latest = float(hist[-1]["v"])
        target = datetime.now() - timedelta(days=days)
        before = after = None
        for h in hist:
            hd = parse_date(h["d"])
            if hd <= target:
                before = h
            elif after is None:
                after = h
                break
        if before and after:
            bd, ad = parse_date(before["d"]), parse_date(after["d"])
            span = (ad - bd).days
            off  = (target - bd).days
            ratio = off / span if span > 0 else 0
            past = float(before["v"]) + ratio * (float(after["v"]) - float(before["v"]))
        elif before:
            past = float(before["v"])
        elif after:
            past = float(after["v"])
        else:
            return None
        return round((latest - past) / past * 100, 2) if past else None
    except Exception:
        return None


def calc_volatility(data, n=90):
    """Daily stdev of returns over last n NAV points. data is newest-first."""
    try:
        recent = data[:min(n, len(data))]
        if len(recent) < 10:
            return None
        returns = [
            (float(recent[i]["nav"]) - float(recent[i + 1]["nav"])) / float(recent[i + 1]["nav"]) * 100
            for i in range(len(recent) - 1) if float(recent[i + 1]["nav"]) > 0
        ]
        if not returns:
            return None
        mean = sum(returns) / len(returns)
        var  = sum((r - mean) ** 2 for r in returns) / len(returns)
        return round(var ** 0.5, 3)
    except Exception:
        return None


def compress_history(data, days=1900, max_pts=200):
    """Keep last 6 months at full daily resolution (accurate 1M/6M),
    sample older data down to stay within max_pts."""
    try:
        cutoff        = datetime.now() - timedelta(days=days)
        recent_cutoff = datetime.now() - timedelta(days=180)
        filtered = list(reversed([d for d in data if parse_date(d["date"]) >= cutoff]))
        if not filtered:
            return []
        recent = [d for d in filtered if parse_date(d["date"]) >= recent_cutoff]
        older  = [d for d in filtered if parse_date(d["date"]) <  recent_cutoff]
        max_old = max(1, max_pts - len(recent))
        step = max(1, len(older) // max_old) if older else 1
        combined = older[::step] + recent
        return [{"d": x["date"], "v": round(float(x["nav"]), 4)} for x in combined]
    except Exception:
        return []


# -- Classification -----------------------------------------------------------
def guess_type(cat):
    c = (cat or "").lower()
    if any(x in c for x in ["debt", "bond", "liquid", "money market", "gilt", "credit"]):
        return "debt"
    if any(x in c for x in ["hybrid", "balanced", "conservative"]):
        return "hybrid"
    if any(x in c for x in ["index", "etf", "nifty", "sensex"]):
        return "index"
    return "equity"


def get_label(cat):
    c = (cat or "").lower()
    if "small cap" in c:                                              return "Small Cap"
    if "large & mid" in c or "large and mid" in c:                    return "Large & Mid Cap"
    if "mid cap" in c or "midcap" in c:                               return "Mid Cap"
    if "large cap" in c or "bluechip" in c:                           return "Large Cap"
    if "flexi cap" in c or "flexicap" in c:                           return "Flexi Cap"
    if "index" in c or "nifty" in c or "sensex" in c or "etf" in c:   return "Index"
    if "sectoral" in c or "thematic" in c or "sector" in c:           return "Sectoral/Thematic"
    return "Equity"


def is_target(cat):
    c = (cat or "").lower()
    return any(t in c for t in TARGET_CATEGORIES)


def is_direct_growth(name):
    n = name.lower()
    return ("direct" in n
            and ("growth" in n or " gr " in n or n.endswith(" gr"))
            and "idcw" not in n and "dividend" not in n
            and "bonus" not in n and "payout" not in n and "reinvest" not in n)


def get_manager_score(house):
    h = (house or "").lower()
    scores = {
        "mirae asset": 1.0, "ppfas": 1.0, "parag parikh": 1.0,
        "nippon india": 0.9, "nippon": 0.9, "dsp": 0.9,
        "kotak": 0.85, "hdfc": 0.85, "axis": 0.85, "canara robeco": 0.85,
        "motilal oswal": 0.85, "motilal": 0.85,
        "sbi": 0.8, "icici prudential": 0.8, "icici": 0.8, "quant": 0.8,
        "aditya birla sun life": 0.8, "aditya birla": 0.8,
        "tata": 0.75, "uti": 0.75, "franklin templeton": 0.75,
        "franklin": 0.75, "canara": 0.75,
        "invesco": 0.7, "bandhan": 0.7, "sundaram": 0.7,
        "pgim": 0.7, "edelweiss": 0.7, "navi": 0.65,
    }
    for key, score in sorted(scores.items(), key=lambda x: -len(x[0])):
        if key in h:
            return score
    return 0.5


def percentile_rank(val, vals):
    return round(sum(1 for v in vals if v < val) / len(vals) * 100, 1) if vals else 50.0


# -- Scoring ------------------------------------------------------------------
def enrich_and_score(funds):
    for f in funds:
        vol, r1y = f.get("vol"), f.get("r1y")
        f["sharpe"] = round((r1y - RF) / (vol * math.sqrt(252)), 3) \
            if (r1y is not None and vol and vol > 0) else None

    cat_sh = defaultdict(list)
    for f in funds:
        if f.get("sharpe") is not None:
            cat_sh[f["label"]].append(f["sharpe"])
    cat_avg = {c: round(sum(v) / len(v), 3) for c, v in cat_sh.items()}

    by_cat = defaultdict(list)
    for f in funds:
        if f.get("r1y") is not None and f.get("sharpe") is not None:
            by_cat[f["label"]].append(f)

    for cat, cf in by_cat.items():
        r1y_v = [f["r1y"] for f in cf]
        sh_v  = [f["sharpe"] for f in cf]
        for f in cf:
            f["r1y_pct"]        = percentile_rank(f["r1y"], r1y_v)
            f["sharpe_pct"]     = percentile_rank(f["sharpe"], sh_v)
            f["above_avg"]      = f["r1y_pct"] > 50 and f["sharpe_pct"] > 50
            f["cat_avg_sharpe"] = cat_avg.get(cat)
            f["manager_score"]  = get_manager_score(f.get("house", ""))
            f["score"] = round(
                (f["r1y_pct"] * 0.40) + (f["sharpe_pct"] * 0.35)
                + (f["manager_score"] * 25 * 0.25), 2)
    return funds


def build_record(code, name, meta, nav_data):
    cat  = meta.get("scheme_category", "")
    hist = compress_history(nav_data)
    return {
        "schemeCode": code,
        "name":  meta.get("scheme_name", name),
        "house": meta.get("fund_house", ""),
        "category": cat,
        "type":  guess_type(cat),
        "label": get_label(cat),
        "nav":   round(float(nav_data[0]["nav"]), 4),
        "vol":   calc_volatility(nav_data),
        "r1m":   calc_return_interp(hist, 30),
        "r6m":   calc_return_interp(hist, 180),
        "r1y":   calc_return_interp(hist, 365),
        "r3y":   calc_return_interp(hist, 1095),
        "r5y":   calc_return_interp(hist, 1825),
        "navHistory": hist,
    }


# -- Main ---------------------------------------------------------------------
def main():
    print("=== Fund refresh started %s ===" % datetime.now().strftime("%Y-%m-%d %H:%M"))

    existing, existing_codes = [], set()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                existing = json.load(f).get("funds", [])
            existing_codes = {f["schemeCode"] for f in existing}
            print("Loaded %d existing funds" % len(existing))
        except Exception as e:
            print("Could not read existing data (%s) - starting fresh" % e)

    # The master list is only needed to DISCOVER new funds. If it fails we can
    # still refresh everything we already track, so don't abort the whole run.
    print("Fetching master scheme list...")
    master = None
    for host in ("https://api.mfapi.in/mf", "http://api.mfapi.in/mf"):
        master = fetch_json(host, retries=4, timeout=90, verbose=True)
        if master:
            if host != BASE:
                print("  (fell back to %s)" % host)
            break

    if master:
        direct = [f for f in master if is_direct_growth(f["schemeName"])]
        new    = [f for f in direct if f["schemeCode"] not in existing_codes]
        print("%d direct-growth plans | %d not yet tracked" % (len(direct), len(new)))
    else:
        new = []
        print("WARNING: could not fetch the master list.")
        if not existing:
            print("FATAL: no master list and no existing data - nothing to do.")
            sys.exit(1)
        print("Continuing anyway: refreshing the %d funds already tracked." % len(existing))
        print("New-fund discovery is skipped this run; it will retry tomorrow.")

    # --- Refresh every fund we already track with LIVE NAV data ---
    print("\nRefreshing NAV for %d tracked funds..." % len(existing))
    refreshed, failed = [], 0
    for i, f in enumerate(existing):
        if i % 50 == 0:
            print("  %d/%d" % (i, len(existing)))
        code = f.get("schemeCode")
        data = fetch_json("%s/%s" % (BASE, code), verbose=(failed < 5)) if code else None
        if not data or not data.get("data"):
            failed += 1
            refreshed.append(f)          # keep the stale record rather than dropping it
            time.sleep(PAUSE)
            continue
        refreshed.append(build_record(code, f.get("name", ""), data.get("meta", {}), data["data"]))
        time.sleep(PAUSE)
    print("  Refreshed %d, %d kept stale (API errors)" % (len(refreshed) - failed, failed))
    if existing and failed == len(existing):
        print("FATAL: every single fetch failed - mfapi.in is unreachable from this runner.")
        print("Leaving fund_data.json untouched rather than rewriting it with stale values.")
        sys.exit(1)

    # --- Discover newly launched funds in our target categories ---
    print("\nChecking %d new schemes..." % len(new))
    added = []
    for i, fund in enumerate(new):
        if i % 200 == 0:
            print("  %d/%d (%d added)" % (i, len(new), len(added)))
        data = fetch_json("%s/%s" % (BASE, fund["schemeCode"]))
        if not data or not data.get("data"):
            time.sleep(0.08)
            continue
        meta = data.get("meta", {})
        if not is_target(meta.get("scheme_category", "")):
            time.sleep(0.08)
            continue
        added.append(build_record(fund["schemeCode"], fund["schemeName"], meta, data["data"]))
        time.sleep(PAUSE)
    print("  Added %d new funds" % len(added))

    # --- Score and save ---
    all_funds = enrich_and_score(refreshed + added)
    all_funds.sort(key=lambda x: (x["label"], x["name"]))

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(all_funds),
        "funds": all_funds,
    }
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, DATA_FILE)   # atomic - never leaves a truncated file

    size_kb = os.path.getsize(DATA_FILE) // 1024
    print("\nSaved %d funds -> fund_data.json (%d KB)" % (len(all_funds), size_kb))
    for cat, n in sorted(Counter(f["label"] for f in all_funds).items()):
        print("  %-22s %d" % (cat, n))
    print("Done.")


if __name__ == "__main__":
    main()
