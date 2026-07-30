#!/usr/bin/env python3
"""
plex-poster-fix
Find Plex movies/shows stuck showing a locally-generated video-frame
thumbnail instead of a real downloaded poster, and reselect a real one.

Root cause: Plex's metadata agent sometimes doesn't lock in a poster before
the item is browsable (new adds, agent mismatch, slow network). When that
happens Plex falls back to a frame grabbed from the video itself
("provider": "local" in the poster candidate list) and displays that as the
poster. If an overlay tool (Kometa / Plex-Meta-Manager) processes the item
before it's fixed, that frame grab gets permanently baked into a new
"upload://" poster with badges stamped on it — indistinguishable from a
correctly-overlaid poster without opening the image.

This script is interactive by default: it walks you through picking a
library, shows you counts, and asks before changing anything. It's also
fully scriptable for cron via flags (see --help / README).
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "poster_fix.log")

# Providers that mean "a real downloaded poster exists" — never touch these.
GOOD_PROVIDERS = ("tmdb", "tvdb", "imdb", "gracenote", "fanarttv")

# Kometa/Plex-Meta-Manager tags every item it overlays with this Plex label
# so it can skip already-processed items on later runs. It must come off a
# fixed item or the overlay tool will never re-stamp the corrected poster.
DEFAULT_OVERLAY_LABEL = "Overlay"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


class Plex:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def get_json(self, path):
        req = Request(f"{self.url}{path}{'&' if '?' in path else '?'}X-Plex-Token={self.token}",
                       headers={"Accept": "application/json"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def sections(self):
        data = self.get_json("/library/sections")
        return [d for d in data["MediaContainer"]["Directory"] if d["type"] in ("movie", "show")]

    def items(self, section_key, item_type):
        data = self.get_json(f"/library/sections/{section_key}/all?type={item_type}")
        return data["MediaContainer"].get("Metadata", [])

    def metadata(self, rating_key):
        data = self.get_json(f"/library/metadata/{rating_key}")
        return data["MediaContainer"]["Metadata"][0]

    def poster_candidates(self, rating_key):
        data = self.get_json(f"/library/metadata/{rating_key}/posters")
        return data["MediaContainer"].get("Metadata", [])

    def select_poster(self, rating_key, candidate_key, retries=2):
        url = f"{self.url}/library/metadata/{rating_key}/posters?url={quote(candidate_key, safe='')}&X-Plex-Token={self.token}"
        for attempt in range(retries + 1):
            try:
                req = Request(url, method="POST")
                with urlopen(req, timeout=30) as resp:
                    return resp.status in (200, 204)
            except (URLError, HTTPError):
                if attempt == retries:
                    raise
                time.sleep(1)

    def labels(self, rating_key):
        return [l["tag"] for l in self.metadata(rating_key).get("Label", [])]

    def remove_label(self, section_key, rating_key, label):
        url = (f"{self.url}/library/sections/{section_key}/all?type=1&id={rating_key}"
               f"&label%5B%5D.tag.tag-={quote(label, safe='')}&X-Plex-Token={self.token}")
        req = Request(url, method="PUT")
        with urlopen(req, timeout=30) as resp:
            return resp.status in (200, 204)

    def transcoded_thumb(self, thumb_path, width=200, height=300):
        src = quote(f"{self.url}{thumb_path}?X-Plex-Token={self.token}", safe="")
        url = f"{self.url}/photo/:/transcode?width={width}&height={height}&minSize=1&url={src}&X-Plex-Token={self.token}"
        with urlopen(url, timeout=30) as resp:
            return resp.read()

    def machine_identifier(self):
        return self.get_json("/identity")["MediaContainer"]["machineIdentifier"]

    def web_link(self, machine_id, rating_key):
        return f"{self.url}/web/index.html#!/server/{machine_id}/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"


def load_config(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_config(path, cfg):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def wizard(path):
    print("No config found — let's set one up.")
    print("Plex token help: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/\n")
    url = input("Plex server URL (e.g. http://192.168.1.10:32400): ").strip().rstrip("/")
    token = input("Plex token: ").strip()
    plex = Plex(url, token)
    try:
        secs = plex.sections()
    except (URLError, HTTPError) as e:
        raise SystemExit(f"Could not reach Plex at {url}: {e}")
    print(f"Connected. Found {len(secs)} movie/show librar{'y' if len(secs)==1 else 'ies'}.")
    cfg = {"plex_url": url, "plex_token": token, "overlay_label": DEFAULT_OVERLAY_LABEL}
    save_config(path, cfg)
    print(f"Saved to {path}\n")
    return cfg


def classify(candidates):
    selected = next((c for c in candidates if c.get("selected")), None)
    if selected is None:
        return "no_selection", selected
    provider = selected.get("provider")
    if provider == "local":
        return "broken_local", selected
    if provider is None and selected.get("ratingKey", "").startswith("upload://"):
        return "kometa_upload", selected
    return "ok", selected


def best_real_candidate(candidates):
    for c in candidates:
        if c.get("provider") in GOOD_PROVIDERS:
            return c
    return None


def scan(plex, section, item_type):
    items = plex.items(section["key"], item_type)
    log(f"Section '{section['title']}': scanning {len(items)} items")
    broken_local, kometa_upload = [], []
    for i, item in enumerate(items, 1):
        rk = str(item["ratingKey"])
        title, year = item.get("title"), item.get("year")
        try:
            candidates = plex.poster_candidates(rk)
        except (URLError, HTTPError) as e:
            log(f"  posters fetch failed for {title} ({year}): {e}")
            continue
        state, _ = classify(candidates)
        if state == "broken_local":
            broken_local.append((section["key"], rk, title, year, candidates))
        elif state == "kometa_upload":
            kometa_upload.append((section["key"], rk, title, year, candidates))
        if i % 250 == 0:
            log(f"  ...scanned {i}/{len(items)}")
    return broken_local, kometa_upload


def fix_one(plex, overlay_label, section_key, rk, title, year, candidates, tag="", capture=None):
    real = best_real_candidate(candidates)
    if not real:
        log(f"  no real poster candidate for {title} ({year}), skipping")
        return False

    try:
        if not plex.select_poster(rk, real["ratingKey"]):
            log(f"  select call failed for {title} ({year})")
            return False
    except (URLError, HTTPError) as e:
        log(f"  select call failed for {title} ({year}): {e}")
        return False
    log(f"  fixed{tag}: {title} ({year}) -> provider={real['provider']}")
    try:
        if overlay_label in plex.labels(rk):
            plex.remove_label(section_key, rk, overlay_label)
            log(f"    removed '{overlay_label}' label so the overlay tool reprocesses it")
    except (URLError, HTTPError) as e:
        log(f"    label check/remove failed for {title} ({year}): {e}")

    if capture is not None:
        capture.append({"rk": rk, "title": title, "year": year})

    return True


def send_notification(plex, notify_cfg, run_label, captured):
    if not captured:
        return
    try:
        machine_id = plex.machine_identifier()
    except (URLError, HTTPError) as e:
        log(f"  notification email FAILED: could not fetch machine identifier: {e}")
        return

    rows = []
    for item in captured:
        link = plex.web_link(machine_id, item["rk"])
        rows.append(
            f'<li><a href="{link}">{item["title"]} ({item["year"]})</a></li>'
        )
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif">'
        f"<h2>{run_label}: {len(captured)} poster{'s' if len(captured) != 1 else ''} fixed</h2>"
        f'<ul>{"".join(rows)}</ul>'
        "</div>"
    )

    payload = {
        "from": notify_cfg.get("from", "Plex Poster Fix <onboarding@resend.dev>"),
        "to": [notify_cfg["to"]],
        "subject": f"{run_label}: {len(captured)} poster(s) corrected",
        "html": html,
    }
    req = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        method="POST",
        # Resend sits behind Cloudflare, which blocks the default urllib
        # User-Agent on some POSTs (Cloudflare error 1010).
        headers={
            "Authorization": f"Bearer {notify_cfg['resend_api_key']}",
            "Content-Type": "application/json",
            "User-Agent": "plex-poster-fix/1.0",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            log(f"  notification email sent (HTTP {resp.status})")
    except (URLError, HTTPError) as e:
        log(f"  notification email FAILED: {e}")


def run_post_fix_hook(cfg):
    hook = cfg.get("post_fix_hook")
    if not hook:
        return
    log(f"  running post_fix_hook: {hook}")
    try:
        result = subprocess.run(hook, shell=True, capture_output=True, text=True, timeout=cfg.get("post_fix_hook_timeout", 600))
        for line in (result.stdout or "").splitlines():
            log(f"    [hook] {line}")
        for line in (result.stderr or "").splitlines():
            log(f"    [hook stderr] {line}")
        if result.returncode != 0:
            log(f"  post_fix_hook exited with status {result.returncode}")
    except subprocess.TimeoutExpired:
        log("  post_fix_hook timed out")


def cmd_fix_rating_key(plex, cfg, rk):
    meta = plex.metadata(rk)
    title, year = meta.get("title"), meta.get("year")
    candidates = plex.poster_candidates(rk)
    section_key = meta.get("librarySectionID")
    notify_cfg = cfg.get("notify")
    capture = [] if notify_cfg else None
    if fix_one(plex, cfg.get("overlay_label", DEFAULT_OVERLAY_LABEL), section_key, rk, title, year, candidates,
               capture=capture):
        print("Fixed. Re-run your overlay tool so it re-stamps badges on the corrected poster.")
        if notify_cfg and capture:
            send_notification(plex, notify_cfg, "Plex Poster Fix (manual)", capture)
        run_post_fix_hook(cfg)
    else:
        print("Could not fix — no real poster candidate found, or the API call failed.")


def cmd_sample(plex, pool, n, outdir):
    os.makedirs(outdir, exist_ok=True)
    picks = random.sample(pool, min(n, len(pool)))
    manifest = []
    for section_key, rk, title, year, _ in picks:
        try:
            meta = plex.metadata(rk)
            thumb = meta.get("thumb")
            if not thumb:
                continue
            img = plex.transcoded_thumb(thumb)
            fname = f"{rk}.jpg"
            with open(os.path.join(outdir, fname), "wb") as f:
                f.write(img)
            manifest.append(f"{rk} : {title} ({year}) -> {fname}")
        except (URLError, HTTPError) as e:
            log(f"  sample fetch failed for {title} ({year}): {e}")
    with open(os.path.join(outdir, "manifest.txt"), "w") as f:
        f.write("\n".join(manifest) + "\n")
    print(f"\nSaved {len(manifest)} thumbnails to {outdir}/ (see manifest.txt).")
    print("Open them and check for video-frame grabs. For any that are bad, fix with:")
    print(f"  python3 {sys.argv[0]} --rating-key <ratingKey>\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    ap.add_argument("--yes", action="store_true", help="skip interactive prompts, assume yes")
    ap.add_argument("--fix", action="store_true", help="fix confirmed-broken (local-thumb) items")
    ap.add_argument("--include-kometa", action="store_true",
                     help="also force-reset ambiguous overlay-tool-touched items (use with care)")
    ap.add_argument("--sample", type=int, metavar="N",
                     help="download N random ambiguous posters to ./review/ for manual inspection instead of fixing")
    ap.add_argument("--rating-key", metavar="RK", help="fix one specific Plex ratingKey directly, skip the scan")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if cfg is None:
        if args.yes:
            raise SystemExit(f"No config at {args.config} and --yes given — run once interactively first.")
        cfg = wizard(args.config)

    plex = Plex(cfg["plex_url"], cfg["plex_token"])
    overlay_label = cfg.get("overlay_label", DEFAULT_OVERLAY_LABEL)

    if args.rating_key:
        cmd_fix_rating_key(plex, cfg, args.rating_key)
        return

    sections = plex.sections()
    if not sections:
        raise SystemExit("No movie/show libraries found on this server.")

    if args.yes:
        chosen = sections
    else:
        print("\nLibraries:")
        for i, s in enumerate(sections, 1):
            print(f"  [{i}] {s['title']} ({s['type']})")
        print(f"  [{len(sections)+1}] All")
        pick = input(f"Scan which? [1-{len(sections)+1}, default {len(sections)+1}]: ").strip()
        if not pick or pick == str(len(sections) + 1):
            chosen = sections
        else:
            chosen = [sections[int(pick) - 1]]

    all_broken, all_kometa = [], []
    for section in chosen:
        item_type = 1 if section["type"] == "movie" else 2
        broken, kometa = scan(plex, section, item_type)
        all_broken += broken
        all_kometa += kometa

    log(f"Confirmed broken (raw video-frame thumbnail selected): {len(all_broken)}")
    for _, rk, title, year, _ in all_broken:
        log(f"  [{rk}] {title} ({year})")

    log(f"Overlay-tool-touched, unverified (may already be fine): {len(all_kometa)}")
    for _, rk, title, year, _ in all_kometa:
        log(f"  [{rk}] {title} ({year})")

    notify_cfg = cfg.get("notify")
    capture = [] if notify_cfg else None
    total_fixed = 0

    do_fix = args.fix
    if not args.yes and not args.fix and all_broken:
        do_fix = input(f"\nFix {len(all_broken)} confirmed-broken items now? [y/N]: ").strip().lower() == "y"

    if do_fix:
        fixed = skipped = 0
        for section_key, rk, title, year, candidates in all_broken:
            if fix_one(plex, overlay_label, section_key, rk, title, year, candidates, capture=capture):
                fixed += 1
            else:
                skipped += 1
        log(f"=== done. fixed={fixed} skipped={skipped} ===")
        log("Re-run your overlay tool now so it re-stamps badges on the corrected posters.")
        total_fixed += fixed

    if args.sample:
        cmd_sample(plex, all_kometa, args.sample, os.path.join(BASE_DIR, "review"))
    elif args.include_kometa:
        proceed = args.yes or input(
            f"\nForce-reset all {len(all_kometa)} overlay-touched items? This discards any "
            "deliberately-picked posters and forces a full overlay re-run. [y/N]: "
        ).strip().lower() == "y"
        if proceed:
            fixed = skipped = 0
            for section_key, rk, title, year, candidates in all_kometa:
                if fix_one(plex, overlay_label, section_key, rk, title, year, candidates, tag=" (overlay-touched)",
                           capture=capture):
                    fixed += 1
                else:
                    skipped += 1
            log(f"=== overlay-touched pass done. fixed={fixed} skipped={skipped} ===")
            total_fixed += fixed
    elif all_kometa and not args.yes:
        print(f"\n{len(all_kometa)} items already have an overlay-tool poster and can't be verified from "
              "metadata alone. Re-run with --sample 25 to spot-check a random sample, or --include-kometa "
              "to force-reset all of them (not recommended unless you've confirmed a real problem).")

    if notify_cfg and capture:
        send_notification(plex, notify_cfg, f"Plex Poster Fix ({time.strftime('%Y-%m-%d')})", capture)

    if total_fixed > 0:
        run_post_fix_hook(cfg)


if __name__ == "__main__":
    main()
