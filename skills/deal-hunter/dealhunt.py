#!/usr/bin/env python3
"""deal-hunter engine: validate, score, dedupe, rank and remember listings for ANY item.

What to hunt is described by a profile (profiles/<name>.json); this file knows nothing about
specific products. Listings are extracted by Claude and passed in as JSON.
Unknown facts must be null: they are never guessed, score 0 and show as UNVERIFIED.
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# User data (own profiles, history) lives outside the plugin so updates never wipe it.
DATA = Path(os.environ.get("DEAL_HUNTER_HOME") or Path.home() / ".deal-hunter")
UNV = "UNVERIFIED"
NEW_WORDS = ("new", "brand new", "factory sealed", "sealed")


# ---------- helpers ----------
def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def get(d, dotted):
    """Read 'a.b.c' from nested dicts; None if any part is missing."""
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def is_new(l):
    return (l.get("condition") or "").lower() in NEW_WORDS


def total_price(l):
    p = l.get("price")
    return None if p is None else round(p + (l.get("shipping") or 0), 2)


def profile_dirs():
    """User profiles win over bundled ones with the same name."""
    return [DATA / "profiles", HERE / "profiles"]


def load_profile(name_or_path):
    p = Path(name_or_path)
    if p.suffix:
        return load_json(p)
    for d in profile_dirs():
        f = d / f"{name_or_path}.json"
        if f.exists():
            return load_json(f)
    raise SystemExit(f"profile '{name_or_path}' not found in: " + ", ".join(str(d) for d in profile_dirs()))


def list_profiles():
    seen, out = set(), []
    for d in profile_dirs():
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            if f.name.startswith("_") or f.stem in seen:
                continue
            seen.add(f.stem)
            prof = load_json(f)
            out.append((f.stem, prof.get("title", ""), prof.get("aliases", [])))
    return out


# ---------- lookup (known-model spec table) ----------
def apply_lookup(l, prof):
    """Return (listing copy with verified attrs merged, hit?)."""
    m = norm(l.get("model"))
    for rule in prof.get("lookup", []):
        if re.match(rule["re"], m):
            return {**l, **rule["set"]}, True
    return l, False


def specs_verified(l, hit):
    return hit or l.get("spec_source") == "datasheet"


# ---------- hard rules ----------
OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}


def hard_filter(l, prof):
    """Return a list of rejection reasons (empty list = passes)."""
    why = []
    title = (l.get("title") or "").lower()
    for kw in prof.get("reject_keywords", []):
        if re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", title):
            why.append(f"title contains '{kw}'")
    must = prof.get("title_must_match_any", [])
    if must and not any(re.search(rx, l.get("title") or "", re.I) for rx in must):
        why.append("title does not match the wanted item/variant")
    l2, _ = apply_lookup(l, prof)
    for r in prof.get("rules", []):
        v = get(l2, r["field"])
        if v is None:
            continue  # unknown is handled by 'verify', never assumed to pass
        if not OPS[r["op"]](v, r["value"]):
            why.append(r.get("label") or f"{r['field']}={v} fails {r['op']} {r['value']}")
    cond = (l.get("condition") or "").lower()
    allowed = [c.lower() for c in prof.get("allowed_conditions", [])]
    if cond and allowed and cond not in allowed:
        why.append(f"condition '{cond}'")
    if l.get("ships_ok") is False:
        why.append(f"cannot ship to {prof.get('ship_country', 'buyer')}")
    if l.get("active") is False:
        why.append("listing not active/purchasable")
    tp = total_price(l)
    if tp is None:
        why.append("no price")
    elif tp > prof["budget"]:
        why.append(f"total {tp} over budget {prof['budget']}")
    return why


def unverified_fields(l, prof):
    out = []
    l2, hit = apply_lookup(l, prof)
    new = is_new(l)
    for f in prof.get("verify", []):
        if f == "@specs":
            if not specs_verified(l, hit):
                out.append("manufacturer specs")
            continue
        if new and f in prof.get("verify_skip_if_new", []):
            continue
        if get(l2, f) is None:
            out.append(f)
    if l.get("shipping") is None:
        out.append("shipping cost")
    if l.get("ships_ok") is None:
        out.append("shipping to buyer's country")
    if l.get("active") is not True:
        out.append("listing active")
    return out


# ---------- scoring ----------
def metric_points(l2, m, new):
    v = get(l2, m["field"])
    w = m["weight"]
    if v is None:
        return w * m["new_fraction"] if new and "new_fraction" in m else 0.0
    if "map" in m:
        return w * m["map"].get(str(v).lower(), m.get("map_default", 0))
    high = m.get("higher_better", False)
    for thr, frac in m["bands"]:
        if (v >= thr) if high else (v <= thr):
            return w * frac
    return w * m.get("else", 0)


def seller_fraction(l):
    """0..1 from rating, volume, returns, warranty (each only if known)."""
    pts = 0
    r, fb, ret, war = (l.get(k) for k in ("seller_rating_pct", "seller_feedback_count",
                                          "return_days", "warranty_days"))
    if r is not None:
        pts += 5 if r >= 99 else 4 if r >= 98 else 2 if r >= 95 else 0
    if fb is not None:
        pts += 2 if fb >= 500 else 1 if fb >= 100 else 0
    if ret is not None:
        pts += 4 if ret >= 30 else 2 if ret >= 14 else 0
    if war is not None:
        pts += 4 if war >= 365 else 3 if war >= 90 else 1 if war >= 30 else 0
    return min(pts, 15) / 15


def score_listing(l, prof):
    """Return (total 0-100, breakdown). Weights are normalised to 100."""
    sc = prof["scoring"]
    l2, _ = apply_lookup(l, prof)
    new = is_new(l)
    parts, weights = {}, {}
    tp = total_price(l)
    pw = sc.get("price_weight", 20)
    unit = sc.get("price_unit_field")
    unit_val = get(l2, unit) if unit else None
    if tp is not None and (not unit or unit_val):
        x = tp / unit_val if unit else tp
        best, worst = sc["price_best"], sc["price_worst"]
        frac = max(0.0, min(1.0, (worst - x) / (worst - best)))
    else:
        frac = 0
    parts["price"], weights["price"] = pw * frac, pw
    sw = sc.get("seller_weight", 15)
    parts["seller"], weights["seller"] = sw * seller_fraction(l), sw
    for m in sc.get("metrics", []):
        parts[m["name"]] = metric_points(l2, m, new)
        weights[m["name"]] = m["weight"]
    total_w = sum(weights.values()) or 1
    k = 100 / total_w
    parts = {n: round(v * k, 1) for n, v in parts.items()}
    return round(sum(parts.values()), 1), parts


# ---------- identity / dedupe ----------
def fingerprint(l):
    return "|".join(str(x).lower() for x in (
        l.get("platform"), l.get("seller"), norm(l.get("model") or l.get("title")), l.get("price")))


def listing_key(l):
    url = (l.get("url") or "").strip()
    m = re.search(r"/itm/(?:[^/?]+/)?(\d{9,})", url)
    if m:
        return "ebay:" + m.group(1)
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if m:
        return "amazon:" + m.group(1)
    if url:
        u = re.sub(r"^https?://(www\.)?", "", url.split("?")[0].split("#")[0]).rstrip("/").lower()
        return "url:" + u
    return "fp:" + fingerprint(l)


def dedupe(listings):
    keys, fps, out = set(), set(), []
    for l in listings:
        k, fp = listing_key(l), fingerprint(l)
        if k in keys or (l.get("seller") and fp in fps):
            continue
        keys.add(k)
        fps.add(fp)
        out.append(l)
    return out


# ---------- history ----------
def load_history(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_history(path, hist):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(hist, indent=1, ensure_ascii=False), encoding="utf-8")


# ---------- pipeline ----------
def run(listings, prof, history=None, today=None):
    today = today or datetime.date.today().isoformat()
    history = history if history is not None else {}
    batch = dedupe(listings)
    res = {"verified": [], "needs_confirmation": [], "rejected": [], "skipped_seen_rejected": [],
           "duplicates_dropped": len(listings) - len(batch)}
    for l in batch:
        key, tp = listing_key(l), total_price(l)
        prev = history.get(key)
        if prev and prev["status"] == "rejected" and prev.get("total") == tp:
            prev["last_seen"] = today
            res["skipped_seen_rejected"].append(l)
            continue
        reasons = hard_filter(l, prof)
        entry = {"first_seen": prev["first_seen"] if prev else today, "last_seen": today,
                 "total": tp, "title": l.get("title"), "url": l.get("url")}
        if reasons:
            l["_reasons"] = reasons
            entry.update(status="rejected", reasons=reasons)
            res["rejected"].append(l)
        else:
            unv = unverified_fields(l, prof)
            score, parts = score_listing(l, prof)
            l.update(_score=score, _parts=parts, _unverified=unv, _key=key, _total=tp,
                     _seen_before=bool(prev))
            entry.update(status="candidate", score=score)
            (res["needs_confirmation"] if unv else res["verified"]).append(l)
        history[key] = entry
    for k in ("verified", "needs_confirmation"):
        res[k].sort(key=lambda x: -x["_score"])
    return res


def top(res, n):
    both = sorted(res["verified"] + res["needs_confirmation"], key=lambda x: -x["_score"])[:n]
    ids = {id(x) for x in both}
    return ([x for x in res["verified"] if id(x) in ids],
            [x for x in res["needs_confirmation"] if id(x) in ids])


# ---------- output ----------
def cell(v, suffix=""):
    return UNV if v is None else f"{v}{suffix}"


def table(rows, prof):
    disp = prof.get("display", [])
    cur = prof.get("currency", "$")
    head = ["#", "Item", "Cond"] + [d["label"] for d in disp] + [f"Total {cur}", "Seller / platform",
                                                                     "Returns / warranty", "Score", "URL"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for i, l in enumerate(rows, 1):
        l2, _ = apply_lookup(l, prof)
        extra = [cell(get(l2, d["field"])) for d in disp]
        ship = "" if l.get("shipping") is not None else " +ship?"
        row = [str(i), f"{l.get('brand', '')} {l.get('model') or l.get('title')}".strip(),
               cell(l.get("condition"))] + extra + [
            f"{l['_total']}{ship}", f"{l.get('seller') or UNV} ({l.get('platform')})",
            f"ret {cell(l.get('return_days'), 'd')} / war {cell(l.get('warranty_days'), 'd')}",
            str(l["_score"]), str(l.get("url"))]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def render(res, prof, n):
    ver, unv = top(res, n)
    out = [f"# {prof.get('title', prof.get('name'))}: top {n}", "",
           "## VERIFIED (all key facts confirmed)\n", table(ver, prof) if ver else "_none_\n",
           "## NEEDS SELLER CONFIRMATION\n", table(unv, prof) if unv else "_none_\n"]
    for l in unv:
        out.append(f"- {l.get('model') or l.get('title')} ({l.get('seller')}): ask about "
                   f"{', '.join(l['_unverified'])}")
    out.append(f"\nRejected: {len(res['rejected'])} | duplicates dropped: {res['duplicates_dropped']} | "
               f"already-rejected skipped: {len(res['skipped_seen_rejected'])}")
    for l in res["rejected"][:15]:
        out.append(f"- x {l.get('title')}: {'; '.join(l['_reasons'])}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("profile", nargs="?", help="profile name (profiles/<name>.json) or path")
    ap.add_argument("candidates", nargs="?", help="JSON file with a list of listings")
    ap.add_argument("--list", action="store_true", help="list available profiles")
    ap.add_argument("--budget", type=float, help="override budget for this run")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.list:
        for n, t, al in list_profiles():
            print(f"{n}: {t} (aliases: {', '.join(al)})")
        return 0
    if not (a.profile and a.candidates):
        ap.error("profile and candidates are required")
    prof = load_profile(a.profile)
    if a.budget:
        prof["budget"] = a.budget
    name = prof.get("name") or Path(a.profile).stem
    hpath = DATA / "history" / f"{name}.json"
    hist = {} if a.no_history else load_history(hpath)
    res = run(load_json(a.candidates), prof, hist)
    if not a.no_history:
        save_history(hpath, hist)
    n = prof.get("top_n", 10)
    if a.json:
        ver, unv = top(res, n)
        print(json.dumps({"verified": ver, "needs_confirmation": unv}, indent=1))
    else:
        print(render(res, prof, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
