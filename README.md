# plex-poster-fix

Find Plex movies (or shows) that are showing a locally-generated **video-frame
thumbnail** instead of a real downloaded poster, and fix them — without
clobbering posters that are actually fine.

## The bug

Plex's metadata agent doesn't always lock in a poster before an item becomes
browsable — new adds, a slow/ambiguous match, network hiccups. When that
happens, Plex falls back to a frame grabbed straight from the video file and
displays *that* as the poster (Plex's own poster picker lists it with
`"provider": "local"`).

If you run an overlay tool like **Kometa** (Plex-Meta-Manager) on a schedule,
it can process that item before you notice — stamping its badges (resolution,
audio, ratings, etc.) permanently onto the video-frame grab. At that point the
poster in Plex looks like a normal overlaid poster, and there's no metadata
field left that says "this started life as a screenshot." You have to open
the image to tell.

This script catches both cases:

| State | What it means | Handling |
|---|---|---|
| `broken_local` | Currently-selected poster is Plex's own local frame grab | **Safe to auto-fix** — no ambiguity |
| `kometa_upload` | Currently-selected poster was uploaded by an overlay tool | **Ambiguous** — could be a fine poster-with-badges, or a Backrooms-style bad frame-with-badges. Flagged for review, never auto-fixed unless you explicitly ask for it |

In one real 8,000-movie library, a random 25-item sample of the "ambiguous"
bucket came back 25/25 clean — so this is usually a rare, one-off race
condition, not a systemic problem. Treat `--include-kometa` as a last resort,
not the default move.

## Requirements

- Python 3.6+, stdlib only — no pip installs.
  - **Unraid users:** install the *NerdPack* / *Nerd Tools* plugin from
    Community Applications, then enable Python 3.
- A Plex server URL and an [X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Setup

```bash
git clone https://github.com/mrjacobarussell/plex-poster-fix.git
cd plex-poster-fix
python3 plex_poster_fix.py
```

First run walks you through an interactive setup wizard (Plex URL + token),
saves it to `config.json` (gitignored — never commit this file), then lets
you pick a library and shows you the counts before touching anything.

## Usage

```bash
# Interactive — pick a library, see counts, decide what to fix
python3 plex_poster_fix.py

# Non-interactive: scan + auto-fix confirmed-broken items only (good for cron)
python3 plex_poster_fix.py --yes --fix

# Spot-check 25 random "ambiguous" posters — downloads small thumbnails to ./review/
python3 plex_poster_fix.py --sample 25

# Fix one specific item by Plex ratingKey (e.g. after spotting a bad one manually)
python3 plex_poster_fix.py --rating-key 486986

# Nuclear option: force-reset every ambiguous overlay-touched item too
python3 plex_poster_fix.py --fix --include-kometa
```

Every fix also strips the overlay tool's tracking label (default `"Overlay"`,
override via `overlay_label` in `config.json`) from the item, so Kometa/PMM
sees it as unprocessed and re-stamps badges onto the corrected poster on its
next run. **Re-run your overlay tool after fixing** — the script does not do
this for you.

## Recommended setup: prevent it going forward

Since `broken_local` items are unambiguous, schedule a `--yes --fix` run
**before** your overlay tool's cron job runs, so new adds get a real poster
before anything bakes a screenshot into place.

On Unraid: *Settings → User Scripts* → new script:

```bash
#!/bin/bash
python3 /path/to/plex-poster-fix/plex_poster_fix.py --yes --fix
```

Schedule it (e.g. daily, `0 3 * * *`), ideally 30–60 minutes before your
Kometa schedule.

## Email notifications

Add a `notify` block to `config.json` (see `config.example.json`) with a
[Resend](https://resend.com) API key, a verified `from` address, and the
`to` address you want alerted:

```json
"notify": {
  "resend_api_key": "re_...",
  "from": "Plex Poster Fix <alerts@yourdomain.com>",
  "to": "you@example.com"
}
```

Whenever a run fixes at least one poster (`--fix`, `--include-kometa`, or
`--rating-key`), it emails a plain list of every title fixed, each one
linking straight to that item in Plex Web on your server. No `notify` block
= no emails, zero behavior change.

Note: Resend requires the `from` address to be on a domain you've verified
with them — it can still send to any recipient (Gmail, etc).

## Chaining another script after a fix (e.g. push the new poster elsewhere)

Set `post_fix_hook` in `config.json` to any shell command. It runs once,
after a run that actually fixed at least one item (never on a run that found
nothing to fix):

```json
"post_fix_hook": "python3 /path/to/some/other/script.py",
"post_fix_hook_timeout": 600
```

Use this to trigger something like a Plex→Jellyfin/Emby poster-sync script
right after this tool corrects a poster, so the fix propagates immediately
instead of waiting for that other tool's own schedule. The hook's stdout/
stderr are captured into `poster_fix.log`. No `post_fix_hook` = nothing runs,
zero behavior change.

## How it works

Plex exposes every poster candidate an agent found (TMDB, TVDB, IMDb,
Gracenote, FanartTV, plus the local frame grab and any manual uploads) via:

```
GET /library/metadata/{ratingKey}/posters
```

each with a `provider` field and a `selected` flag. This script reads that
list, classifies the currently-selected poster, and — when you approve a fix —
POSTs to the same endpoint to select a real candidate:

```
POST /library/metadata/{ratingKey}/posters?url={candidateKey}
```

then removes the overlay label via Plex's tag-edit API:

```
PUT /library/sections/{sectionKey}/all?type=1&id={ratingKey}&label[].tag.tag-=Overlay
```

## License

MIT
