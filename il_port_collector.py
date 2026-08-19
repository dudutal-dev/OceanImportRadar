#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
il_port_collector.py — איסוף אוטומטי של אניות צפויות לנמלי ישראל.

מה הוא עושה
-----------
1. מושך את עמודי הנמלים המוגדרים ב-sources.json
2. מזהה טבלאות בעמוד ומנחש איזו מהן היא טבלת האניות (לפי מילות מפתח בכותרות)
3. ממפה עמודות לסכמה אחידה: vessel / carrier / service / voyage / pol / pod / eta / ata
4. משווה מול הריצה הקודמת (history.json) כדי לזהות **הזזת ETA** — זה הלב של המערכת
5. כותב radar_feed.json + sailings.csv לצד קובץ ה-HTML

שימוש
-----
    pip install requests beautifulsoup4 lxml
    python il_port_collector.py                  # ריצה רגילה
    python il_port_collector.py --inspect ashdod_expected   # הצג מה יש בעמוד
    python il_port_collector.py --only ashdod_expected      # מקור אחד בלבד
    python il_port_collector.py --dry-run                   # בלי לכתוב קבצים

לעמודים שנטענים ב-JavaScript:
    pip install playwright && playwright install chromium
ואז "render": true במקור הרלוונטי.

הערה חשובה
----------
אתרי הנמלים משנים מבנה מדי פעם. הסקריפט לא מקודד selectors קשיחים אלא מזהה
טבלאות לפי תוכן — לכן הוא שורד שינויי עיצוב, אבל אם עמוד עובר לטעינת JS או
משנה שמות עמודות, הרץ --inspect ועדכן את column_keywords ב-sources.json.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, date, timedelta

try:
    import requests
except ImportError:
    sys.exit("חסר requests. הרץ: pip install requests beautifulsoup4 lxml")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("חסר beautifulsoup4. הרץ: pip install beautifulsoup4 lxml")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "sources.json")

FIELDS = ["vessel", "carrier", "service", "voyage", "pol", "pod", "eta", "ata", "etd"]


# ----------------------------------------------------------------- utilities
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"לא נמצא {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def norm(s):
    """נרמול טקסט תא/כותרת להשוואה."""
    if s is None:
        return ""
    s = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", str(s))   # bidi marks
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), (0, 1, 2)),
    (re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})"), (2, 1, 0)),
]


def parse_date(raw, today=None):
    """מחזיר YYYY-MM-DD או '' — תומך בפורמט ישראלי DD/MM וגם DD/MM בלי שנה."""
    s = norm(raw)
    if not s:
        return ""
    today = today or date.today()

    for rx, order in DATE_PATTERNS:
        m = rx.match(s)
        if m:
            g = m.groups()
            y, mo, d = int(g[order[0]]), int(g[order[1]]), int(g[order[2]])
            if y < 100:
                y += 2000
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                return ""

    # DD/MM בלי שנה — נפוץ בלוחות נמל. בוחרים את השנה הקרובה הגיונית.
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})(?!\d)", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        for y in (today.year, today.year + 1, today.year - 1):
            try:
                cand = date(y, mo, d)
            except ValueError:
                continue
            if -120 <= (cand - today).days <= 300:
                return cand.isoformat()
    return ""


def build_header_map(headers, keywords):
    """ממפה אינדקס עמודה -> שם שדה, לפי מילות מפתח."""
    mapping = {}
    taken = set()
    lowered = [norm(h).lower() for h in headers]

    # שלב 1: התאמה מדויקת
    for field, keys in keywords.items():
        for i, h in enumerate(lowered):
            if i in taken or not h:
                continue
            if h in [k.lower() for k in keys]:
                mapping[i] = field
                taken.add(i)
                break
    # שלב 2: הכלה
    for field, keys in keywords.items():
        if field in mapping.values():
            continue
        for i, h in enumerate(lowered):
            if i in taken or not h:
                continue
            if any(k.lower() in h for k in keys):
                mapping[i] = field
                taken.add(i)
                break
    return mapping


def score_table(mapping):
    """כמה סביר שזו טבלת אניות."""
    fields = set(mapping.values())
    score = 0
    if "vessel" in fields:
        score += 5
    if "eta" in fields or "ata" in fields:
        score += 3
    score += len(fields & {"carrier", "voyage", "pol", "pod", "service", "etd"})
    return score


# ----------------------------------------------------------------- fetching
def fetch_html(url, cfg, render=False):
    d = cfg["defaults"]
    if render:
        return fetch_rendered(url, d)
    headers = {
        "User-Agent": d["user_agent"],
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    }
    last = None
    for attempt in range(d.get("retries", 2) + 1):
        try:
            r = requests.get(url, headers=headers, timeout=d.get("timeout", 25))
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt < d.get("retries", 2):
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"הבאת העמוד נכשלה: {last}")


def fetch_rendered(url, d):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "המקור מסומן render=true אבל playwright לא מותקן. "
            "הרץ: pip install playwright && playwright install chromium"
        )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=d["user_agent"], locale="he-IL")
        page.goto(url, timeout=d.get("timeout", 25) * 1000, wait_until="networkidle")
        page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
    return html


# ----------------------------------------------------------------- parsing
def extract_tables(html):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for tbl in soup.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [norm(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if len(rows) >= 2:
            out.append(rows)
    return out


def find_header_row(rows, keywords):
    """הכותרת לא תמיד בשורה הראשונה."""
    best, best_score, best_map = 0, -1, {}
    for i, row in enumerate(rows[:6]):
        m = build_header_map(row, keywords)
        s = score_table(m)
        if s > best_score:
            best, best_score, best_map = i, s, m
    return best, best_map, best_score


def parse_source(html, source, keywords):
    tables = extract_tables(html)
    if not tables:
        return [], "לא נמצאו טבלאות בעמוד"

    scored = []
    for rows in tables:
        hi, mapping, s = find_header_row(rows, keywords)
        scored.append((s, hi, mapping, rows))
    scored.sort(key=lambda x: x[0], reverse=True)
    s, hi, mapping, rows = scored[0]

    if s < 5 or "vessel" not in mapping.values():
        return [], f"לא זוהתה טבלת אניות (ציון {s}). הרץ --inspect {source['name']}"

    records = []
    for row in rows[hi + 1:]:
        rec = {f: "" for f in FIELDS}
        for idx, field in mapping.items():
            if idx < len(row):
                rec[field] = row[idx]
        vessel = norm(rec["vessel"])
        # דלג על שורות סיכום / ריקות
        if not vessel or len(vessel) < 2 or vessel.lower() in ("total", "סה\"כ", "סהכ"):
            continue
        if re.fullmatch(r"[\d\W]+", vessel):
            continue
        rec["vessel"] = vessel
        rec["eta"] = parse_date(rec["eta"])
        rec["ata"] = parse_date(rec["ata"])
        rec["etd"] = parse_date(rec["etd"])
        if not rec["pod"] and source.get("default_pod"):
            rec["pod"] = source["default_pod"]
        rec["source"] = source["name"]
        records.append(rec)

    return records, f"נמצאו {len(records)} שורות (ציון טבלה {s})"


# ----------------------------------------------------------------- history / diff
def key_of(rec):
    return f"{rec['vessel'].strip().lower()}|{norm(rec.get('voyage','')).lower()}"


def load_history(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                            # noqa: BLE001
            log("history.json פגום — מתחיל מחדש")
    return {}


def merge_history(history, records):
    """
    שומר את ה-ETA הראשון שנראה כ-baseline מוצהר, ומעדכן את הנוכחי.
    זה מה שמאפשר לחשב אמינות ספן בלי לשלם לאף אחד.
    """
    now = datetime.now().isoformat(timespec="seconds")
    changes = []
    for rec in records:
        k = key_of(rec)
        h = history.get(k)
        if not h:
            history[k] = {
                "vessel": rec["vessel"], "carrier": rec["carrier"], "service": rec["service"],
                "voyage": rec["voyage"], "pol": rec["pol"], "pod": rec["pod"],
                "eta_declared": rec["eta"], "eta_current": rec["eta"],
                "ata": rec["ata"], "first_seen": now, "last_seen": now,
                "eta_moves": 0,
            }
            continue

        for f in ("carrier", "service", "pol", "pod"):
            if rec[f]:
                h[f] = rec[f]
        if rec["eta"]:
            if not h.get("eta_declared"):
                h["eta_declared"] = rec["eta"]
                h["eta_current"] = rec["eta"]
            elif rec["eta"] != h.get("eta_current"):
                changes.append({
                    "vessel": rec["vessel"], "pod": rec["pod"],
                    "from": h.get("eta_current"), "to": rec["eta"],
                })
                h["eta_current"] = rec["eta"]
                h["eta_moves"] = h.get("eta_moves", 0) + 1
        if rec["ata"] and not h.get("ata"):
            h["ata"] = rec["ata"]
        h["last_seen"] = now

    # ניקוי רשומות ישנות מאוד
    cutoff = (datetime.now() - timedelta(days=180)).isoformat()
    for k in [k for k, v in history.items() if v.get("last_seen", "") < cutoff]:
        del history[k]

    return changes


# ----------------------------------------------------------------- output
def write_outputs(cfg, history, changes, dry_run=False):
    out = cfg["output"]
    feed_path = os.path.join(HERE, out["feed"])
    csv_path = os.path.join(HERE, out["csv"])
    hist_path = os.path.join(HERE, out["history"])

    sailings = []
    for h in history.values():
        sailings.append({
            "vessel": h["vessel"], "carrier": h.get("carrier", ""), "service": h.get("service", ""),
            "voyage": h.get("voyage", ""), "pol": h.get("pol", ""), "pod": h.get("pod", ""),
            "eta": h.get("eta_declared", ""),
            "eta_current": h.get("eta_current", ""),
            "ata": h.get("ata", ""),
        })
    sailings.sort(key=lambda r: r["eta_current"] or r["eta"] or "9999")

    feed = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sailings": sailings,
        "eta_changes": changes,
    }

    if dry_run:
        log(f"[dry-run] היו נכתבות {len(sailings)} הפלגות")
        return

    with open(feed_path, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=1)
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["vessel", "carrier", "service", "voyage",
                                          "pol", "pod", "eta", "eta_current", "ata"])
        w.writeheader()
        w.writerows(sailings)

    log(f"נכתב {out['feed']} · {len(sailings)} הפלגות")
    log(f"נכתב {out['csv']}")


# ----------------------------------------------------------------- inspect
def inspect(cfg, name):
    src = next((s for s in cfg["sources"] if s["name"] == name), None)
    if not src:
        sys.exit(f"לא נמצא מקור בשם {name}")
    log(f"מושך {src['url']}")
    html = fetch_html(src["url"], cfg, src.get("render", False))
    tables = extract_tables(html)
    log(f"נמצאו {len(tables)} טבלאות")
    for i, rows in enumerate(tables):
        hi, mapping, s = find_header_row(rows, cfg["column_keywords"])
        print(f"\n--- טבלה {i} · שורות {len(rows)} · ציון {s} · שורת כותרת {hi} ---")
        print("כותרות:", rows[hi][:14])
        print("מיפוי :", {rows[hi][k] if k < len(rows[hi]) else k: v for k, v in mapping.items()})
        for r in rows[hi + 1: hi + 4]:
            print("  דוגמה:", r[:14])
    if not tables:
        print("\nאין טבלאות ב-HTML הגולמי — סימן שהעמוד נטען ב-JavaScript.")
        print("הוסף \"render\": true למקור הזה ב-sources.json (דורש playwright).")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="איסוף אניות צפויות לנמלי ישראל")
    ap.add_argument("--inspect", metavar="SOURCE", help="הצג את הטבלאות בעמוד ואת המיפוי")
    ap.add_argument("--only", metavar="SOURCE", help="הרץ מקור בודד")
    ap.add_argument("--dry-run", action="store_true", help="בלי לכתוב קבצים")
    args = ap.parse_args()

    cfg = load_config()

    if args.inspect:
        inspect(cfg, args.inspect)
        return

    keywords = cfg["column_keywords"]
    hist_path = os.path.join(HERE, cfg["output"]["history"])
    history = load_history(hist_path)

    all_records, failures = [], []
    sources = [s for s in cfg["sources"] if s.get("enabled")]
    if args.only:
        sources = [s for s in cfg["sources"] if s["name"] == args.only]
        if not sources:
            sys.exit(f"לא נמצא מקור בשם {args.only}")

    for i, src in enumerate(sources):
        log(f"→ {src['label']}")
        try:
            html = fetch_html(src["url"], cfg, src.get("render", False))
            recs, msg = parse_source(html, src, keywords)
            log(f"   {msg}")
            all_records.extend(recs)
        except Exception as e:                        # noqa: BLE001
            log(f"   נכשל: {e}")
            failures.append((src["name"], str(e)))
        if i < len(sources) - 1:
            time.sleep(cfg["defaults"].get("polite_delay_sec", 3))

    if not all_records:
        log("לא נאסף דבר. הרץ --inspect <source> כדי לראות מה יש בעמוד.")
        if failures:
            for n, e in failures:
                log(f"   {n}: {e}")
        return

    changes = merge_history(history, all_records)
    if changes:
        log(f"זוהו {len(changes)} הזזות ETA:")
        for c in changes[:12]:
            log(f"   {c['vessel']} ({c['pod']}): {c['from']} → {c['to']}")
    else:
        log("לא זוהו הזזות ETA מאז הריצה הקודמת")

    write_outputs(cfg, history, changes, args.dry_run)
    log("סיום. פתח את ocean-import-radar.html ולחץ «רענן מהפיד».")


if __name__ == "__main__":
    main()
