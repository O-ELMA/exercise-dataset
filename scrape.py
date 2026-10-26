#!/usr/bin/env python3
"""Scrape the JEFIT exercise database into data/exercises.jsonl plus media files."""

import argparse
import concurrent.futures as cf
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "https://www.jefit.com"
SITEMAP = BASE + "/sitemap.xml"
GIF_URL = "https://cdn.jefit.com/assets/img/exercises/gifs/{}.gif"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "exercises.jsonl")
MEDIA = os.path.join(ROOT, "media")
WORKERS = 6
lock = threading.Lock()
done = 0


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                raise
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"failed after {tries} tries: {url}")


def rsc_payload(page):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S)
    return "".join(json.loads(c) for c in chunks)


def list_exercises():
    """The listing page embeds the whole exercise table as one JSON array."""
    page = get(BASE + "/exercises?page=1").decode("utf-8", "ignore")
    payload = rsc_payload(page)
    start = payload.find("[", payload.find('"exercises":['))
    depth = 0
    for i in range(start, len(payload)):
        depth += {"[": 1, "]": -1}.get(payload[i], 0)
        if depth == 0:
            return json.loads(payload[start : i + 1])
    raise RuntimeError("exercise array not found in listing page")


def pick(pattern, text, group=1):
    m = re.search(pattern, text, re.S)
    return m.group(group).strip() if m else None


def parse_detail(page):
    out = {}
    for label, key in (("Difficulty", "difficulty"), ("Exercise Type", "exercise_type"), ("Log Type", "log_type")):
        out[key] = pick(rf">{label}</span><div[^>]*><p[^>]*>([^<]+)</p>", page) or pick(rf">{label}</span><p[^>]*>([^<]+)</p>", page)
    muscles = {"main": [], "secondary": []}
    start = page.find("Target Muscle Groups")
    segment = page[start : page.find(">Equipment<", start)]
    for _, body in re.findall(r'<a[^>]+href="/exercises/([a-z_]+)"[^>]*>(.*?)</a>', segment, re.S):
        name = pick(r"<p[^>]*>([^<]+)</p>", body)
        if name:
            muscles["main" if ">Main<" in body else "secondary"].append(name)
    out["muscles"] = muscles
    ld = {}
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if data.get("@type") == "Exercise":
            ld = data
            break
    out["ld"] = ld
    out["gif_url"] = pick(r'(https://cdn\.jefit\.com/assets/img/exercises/gifs/\d+\.gif)', page)
    return out


def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest):
        return dest
    data = get(url)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest + ".part", "wb") as f:
        f.write(data)
    os.replace(dest + ".part", dest)
    return dest


def rel(path):
    return os.path.relpath(path, ROOT)


def scrape(ex):
    exid = int(ex["id"].split("_")[1])
    folder = os.path.join(MEDIA, str(exid))
    detail = parse_detail(get(ex["url"]).decode("utf-8", "ignore"))
    record = {
        "id": exid,
        "name": ex["name"],
        "url": ex["url"],
        "description": detail["ld"].get("description") or ex["description"],
        "difficulty": detail["difficulty"] or ex["difficulty"],
        "exercise_type": detail["exercise_type"] or ex["type"],
        "log_type": detail["log_type"] or ex["input_format"],
        "popularity": ex.get("popularity"),
        "equipment": detail["ld"].get("equipment") or ex["equipment"],
        "body_parts": ex["body_parts"],
        "muscles": detail["muscles"],
        "gif": None,
        "images": [],
    }
    if detail["gif_url"]:
        try:
            record["gif"] = {"url": detail["gif_url"], "path": rel(download(detail["gif_url"], os.path.join(folder, f"{exid}.gif")))}
        except urllib.error.HTTPError:
            print(f"  no gif for {exid}", file=sys.stderr)
    for url in ex["media"]["image_urls"]:
        path = os.path.join(folder, url.rsplit("/", 1)[1].split("?")[0])
        try:
            record["images"].append({"url": url, "path": rel(download(url, path))})
        except urllib.error.HTTPError:
            print(f"  missing image {url}", file=sys.stderr)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only scrape the first N exercises (for testing)")
    args = ap.parse_args()

    slugs = dict(re.findall(r"<loc>https://www\.jefit\.com/exercises/(\d+)/([^<]+)</loc>", get(SITEMAP).decode("utf-8", "ignore")))
    exercises = []
    for e in list_exercises():
        num = str(int(e["id"].split("_")[1]))
        if num in slugs:
            exercises.append({**e, "slug": slugs[num], "url": f"{BASE}/exercises/{int(num)}/{slugs[num]}"})

    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    have = set()
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            for line in f:
                try:
                    have.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    pass
    todo = [e for e in exercises if int(e["id"].split("_")[1]) not in have]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(exercises)} exercises, {len(have)} done, {len(todo)} to fetch", file=sys.stderr)

    def work(ex):
        global done
        record = scrape(ex)
        with lock:
            with open(DATA, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(todo)}", file=sys.stderr)
        return record

    with cf.ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(work, e) for e in todo]
        for f in cf.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
