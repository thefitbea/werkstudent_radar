import csv
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import requests

# ---
# 1. CONFIGURATION (EDIT TARGETS HERE)
# ---
TARGET_CITIES = [
    # Primary: Berlin / Potsdam
    "berlin","regensburg", "potsdam", "teltow", "ludwigsfelde", "grünheide", "gruenheide",
    # Secondary: Regensburg / Eastern Bavaria corridor
    "regensburg", "wackersdorf", "pfreimd", "straubing", "plattling",
    "neumarkt", "maxhütte", "burglengenfeld", "schwandorf", "zandt", "roding",
]

BERLIN_CITIES = {"berlin", "potsdam", "teltow", "ludwigsfelde", "grünheide", "gruenheide"}

# Bundesagentur für Arbeit searches: (Hub City, Radius km, Query)
BA_SEARCHES = [
    ("Berlin", 30, "Werkstudent Supply Chain"),
    ("Berlin", 30, "Werkstudent Data"),
    ("Berlin", 30, "Werkstudent SAP"),
    ("Berlin", 30, "Working Student Operations"),
    ("Regensburg", 55, "Werkstudent Logistik"),
    ("Regensburg", 55, "Werkstudent Data"),
    ("Regensburg", 55, "Werkstudent SAP"),
]

# Direct ATS endpoints (keyless public APIs / XML feeds)
SMARTRECRUITERS_COMPANIES = ["Gerresheimer", "BoschGroup", "DreesSommerSE"]
GREENHOUSE_BOARDS = ["gropyus", "grover", "teampicnic"]
PERSONIO_COMPANIES = ["swissbit", "green-fusion"]

MIN_RELEVANCE_SCORE = 3
SEEN_FILE = Path("seen_jobs.json")
CSV_FILE = Path("matched_jobs.csv")
UI_JSON_FILE = Path("docs/jobs.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Industry40JobRadar/1.0)"}

# --
# 2. FILTERING & SCORING ENGINE
# --
STUDENT_TITLE_RE = re.compile(
    r"\b(werkstudent\w*|working\s+student|studentische\s+hilfskraft|"
    r"praktik\w*|intern|internship|studierend\w*)\b",
    re.IGNORECASE,
)

BLACKLIST_TITLE = [
    "schülerpraktikum", "schuelerpraktikum", "ausbildung", "auszubildende",
    "duales studium", "dual study", "lagerhelfer", "kommissionierer",
    "produktionshelfer", "staplerfahrer", "fahrer", "sps-programmierer",
    "tia portal", "inbetriebnahme", "betriebsschlosser", "cnc-fräser",
    "cnc-dreher", "maschinenbediener", "notrufzentrale", "rechtsreferendar",
    "personal und recht", "hr service",
]

# Catches C1/C2/native German without falsely killing "English C1, German B1"
HARD_GERMAN_RE = re.compile(
    r"(deutsch\w*|german)[^,.;\n]{0,25}?\b(c1|c2|verhandlungssicher\w*|muttersprach\w*|native|fluent)\b"
    r"|\b(c1|c2|verhandlungssicher\w*|muttersprach\w*|native|fluent)\b[^,.;\n]{0,25}?(deutsch\w*|german)",
    re.IGNORECASE,
)

SCORE_WEIGHTS = {
    "python": 3, "sql": 3, "sap": 4, "power bi": 3, "erp": 2,
    "supply chain": 3, "logistik": 2, "logistics": 3,
    "order management": 4, "auftragsmanagement": 4,
    "industry 4.0": 3, "industrie 4.0": 3, "iiot": 3, "mes": 2,
    "data": 2, "daten": 2, "analytics": 2, "automation": 2,
    "automatisierung": 2, "process": 1, "prozess": 1,
    "english": 2, "englisch": 1,
}


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def matches_location(location_str: str) -> bool:
    loc = (location_str or "").lower()
    if "remote" in loc and ("germany" in loc or "deutschland" in loc):
        return True
    return any(city in loc for city in TARGET_CITIES)


def evaluate_job(title: str, description: str) -> Tuple[bool, int, str]:
    title_low = title.lower()
    full_text = f"{title}\n{description}".lower()
    if not STUDENT_TITLE_RE.search(title):
        return False, 0, "Not a student/intern title"
    for bad in BLACKLIST_TITLE:
        if bad in title_low:
            return False, 0, f"Blacklisted: {bad}"
    if HARD_GERMAN_RE.search(full_text):
        return False, 0, "Requires C1/native German"
    score = sum(w for kw, w in SCORE_WEIGHTS.items() if kw in full_text)
    if score < MIN_RELEVANCE_SCORE:
        return False, score, f"Low relevance score ({score})"
    flags = []
    if "pflichtpraktikum" in full_text and "werkstudent" not in title_low and "freiwillig" not in full_text:
        flags.append("WARN: Likely Pflichtpraktikum")
    if "führerschein" in full_text or "eigener pkw" in full_text:
        flags.append("WARN: Mentions car/license")
    return True, score, " | ".join(flags) if flags else "CLEAN"


# -----
# 3. API & XML FETCHERS
# ----
def fetch_bundesagentur() -> List[Dict]:
    jobs = []
    url = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
    headers = {**HEADERS, "X-API-Key": "jobboerse-jobsuche"}
    for city, radius, query in BA_SEARCHES:
        for angebotsart in (1, 34):  # 1 = Work/Werkstudent, 34 = Praktikum
            params = {
                "was": query, "wo": city, "umkreis": radius,
                "angebotsart": angebotsart, "pav": "false",
                "zeitarbeit": "false", "veroeffentlichtseit": 14,
                "size": 50, "page": 1,
            }
            try:
                r = requests.get(url, headers=headers, params=params, timeout=15)
                r.raise_for_status()
                for item in r.json().get("stellenangebote", []):
                    ref = item.get("refnr") or item.get("referenznummer", "")
                    ort = item.get("arbeitsort", {}).get("ort", city)
                    link = item.get("externeUrl") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"
                    jobs.append({
                        "id": f"ba:{ref}",
                        "source": "Arbeitsagentur",
                        "company": item.get("arbeitgeber", "Unknown"),
                        "title": item.get("titel") or item.get("beruf", ""),
                        "location": ort,
                        "url": link,
                        "description": f"{item.get('beruf', '')} {item.get('titel', '')}",
                    })
            except Exception as e:
                print(f"[WARN] Bundesagentur ({city}/{query}): {e}")
    return jobs


def fetch_smartrecruiters() -> List[Dict]:
    jobs = []
    for company_id in SMARTRECRUITERS_COMPANIES:
        url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
        try:
            r = requests.get(url, headers=HEADERS, params={"limit": 100, "country": "de"}, timeout=15)
            r.raise_for_status()
            for item in r.json().get("content", []):
                title = item.get("name", "")
                loc_obj = item.get("location", {})
                loc = f"{loc_obj.get('city', '')}, {loc_obj.get('country', '')}"
                if not STUDENT_TITLE_RE.search(title) or not matches_location(loc):
                    continue
                desc = ""
                posting_id = item.get("id")
                try:
                    detail = requests.get(f"{url}/{posting_id}", headers=HEADERS, timeout=10).json()
                    sections = detail.get("jobAd", {}).get("sections", {})
                    desc = clean_html(" ".join(s.get("text", "") for s in sections.values()))
                except Exception:
                    pass
                jobs.append({
                    "id": f"sr:{company_id}:{posting_id}",
                    "source": "SmartRecruiters",
                    "company": item.get("company", {}).get("name", company_id),
                    "title": title,
                    "location": loc,
                    "url": f"https://jobs.smartrecruiters.com/{company_id}/{posting_id}",
                    "description": desc,
                })
        except Exception as e:
            print(f"[WARN] SmartRecruiters ({company_id}): {e}")
    return jobs


def fetch_greenhouse() -> List[Dict]:
    jobs = []
    for board in GREENHOUSE_BOARDS:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        try:
            r = requests.get(url, headers=HEADERS, params={"content": "true"}, timeout=15)
            r.raise_for_status()
            for item in r.json().get("jobs", []):
                loc = item.get("location", {}).get("name", "")
                if not matches_location(loc):
                    continue
                jobs.append({
                    "id": f"gh:{board}:{item.get('id')}",
                    "source": "Greenhouse",
                    "company": board.upper(),
                    "title": item.get("title", ""),
                    "location": loc,
                    "url": item.get("absolute_url", ""),
                    "description": clean_html(item.get("content", "")),
                })
        except Exception as e:
            print(f"[WARN] Greenhouse ({board}): {e}")
    return jobs


def fetch_personio() -> List[Dict]:
    jobs = []
    for sub in PERSONIO_COMPANIES:
        for lang in ("en", "de"):
            url = f"https://{sub}.jobs.personio.de/xml?language={lang}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                if r.status_code != 200 or not r.text.strip().startswith("<"):
                    continue
                root = ET.fromstring(r.content)
                for pos in root.findall("position"):
                    pid = pos.findtext("id", "")
                    title = pos.findtext("name", "")
                    office = pos.findtext("office", "")
                    if not matches_location(office):
                        continue
                    desc_parts = [clean_html(v.text or "") for v in pos.findall(".//jobDescription/value")]
                    jobs.append({
                        "id": f"per:{sub}:{pid}",
                        "source": "Personio",
                        "company": sub.title(),
                        "title": title,
                        "location": office,
                        "url": f"https://{sub}.jobs.personio.de/job/{pid}",
                        "description": " ".join(desc_parts),
                    })
                break
            except Exception as e:
                print(f"[WARN] Personio ({sub}/{lang}): {e}")
    return jobs


# --
# 4. EXPORTS & CHATBOT VIA TELEGRAM OR WHATSAPP
# ---
def export_ui_json(jobs: List[Dict]) -> None:
    UI_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    records = []
    for job in jobs:
        title = job.get("title", "")
        desc = job.get("description", "")
        t_low = title.lower()
        full = f"{title}\n{desc}".lower()
        loc_low = job.get("location", "").lower()

        if not STUDENT_TITLE_RE.search(title):
            continue

        is_werk = any(k in t_low for k in ("werkstudent", "working student", "studentische hilfskraft"))
        is_intern = any(k in t_low for k in ("praktik", "intern"))
        contract = "Werkstudent" if is_werk else ("Praktikum / Intern" if is_intern else "Other Student")

        blacklist_hit = next((b for b in BLACKLIST_TITLE if b in t_low), "")
        is_pflicht = "pflichtpraktikum" in full and not is_werk and "freiwillig" not in full
        requires_c1 = bool(HARD_GERMAN_RE.search(full))
        mentions_en = any(w in full for w in ("english", "englisch"))
        requires_car = any(w in full for w in ("führerschein", "eigener pkw", "eigenes fahrzeug"))
        matched_skills = [kw for kw in SCORE_WEIGHTS if kw in full]
        score = sum(SCORE_WEIGHTS[kw] for kw in matched_skills)

        if any(c in loc_low for c in BERLIN_CITIES):
            region = "Berlin/Potsdam"
        elif "remote" in loc_low:
            region = "Remote DE"
        else:
            region = "Bavaria"

        records.append({
            "id": job["id"],
            "company": job.get("company", ""),
            "title": title,
            "location": job.get("location", ""),
            "region": region,
            "contract": contract,
            "score": score,
            "skills": matched_skills,
            "source": job.get("source", ""),
            "url": job.get("url", ""),
            "snippet": desc[:260].strip(),
            "is_pflicht": is_pflicht,
            "requires_c1": requires_c1,
            "mentions_en": mentions_en,
            "requires_car": requires_car,
            "blacklist_hit": blacklist_hit,
        })

    records.sort(key=lambda x: x["score"], reverse=True)
    payload = {"updated_at": now_iso, "total": len(records), "jobs": records}
    UI_JSON_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def append_csv(matches: List[Dict]) -> None:
    if not matches:
        return
    file_exists = CSV_FILE.exists()
    fields = ["found_at", "score", "company", "title", "location", "flags", "source", "url"]
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        for m in matches:
            writer.writerow({k: m[k] for k in fields})


def send_telegram(matches: List[Dict]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id or not matches:
        return
    lines = [f"🎯 New Student Jobs Found ({len(matches)})\n"]
    for job in matches[:15]:
        lines.append(
            f"• {job['company']} — {job['title']}\n"
            f"  📍 {job['location']} | ⭐ Score: {job['score']} | {job['flags']}\n"
            f"  🔗 {job['url']}\n"
        )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines), "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")


def main() -> None:
    seen = load_seen()
    raw_jobs = (
        fetch_bundesagentur()
        + fetch_smartrecruiters()
        + fetch_greenhouse()
        + fetch_personio()
    )
    unique_jobs = list({j["id"]: j for j in raw_jobs}.values())

    # 1. Refresh full student dataset for the GitHub Pages filter UI
    export_ui_json(unique_jobs)

    # 2. Alert only on brand-new jobs not yet recorded in seen_jobs.json
    new_matches = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for job in unique_jobs:
        if job["id"] in seen:
            continue
        passed, score, note = evaluate_job(job["title"], job["description"])
        seen.add(job["id"])
        if passed:
            new_matches.append({
                "found_at": now, "score": score, "company": job["company"],
                "title": job["title"], "location": job["location"],
                "flags": note, "source": job["source"], "url": job["url"],
            })

    new_matches.sort(key=lambda x: x["score"], reverse=True)
    save_seen(seen)
    append_csv(new_matches)
    send_telegram(new_matches)
    print(f"Scanned {len(unique_jobs)} unique listings | {len(new_matches)} NEW qualified matches.")


if __name__ == "__main__":
    main()
