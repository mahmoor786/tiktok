# Trendpack

A private web page that researches products for a UK TikTok Shop affiliate account and writes a complete video pack for each one: one ready Google Flow prompt for an 8-second video, two opening texts to test, a voiceover line, caption, hashtags and posting time.

She makes the video in Flow and posts it herself. Trendpack does the research and writing.

## How she uses it

1. Open the page on her phone and sign in.
2. Pick a category and tap **Find 10 products**, or paste a product link.
3. Tick the products she wants (up to 5) and tap **Make packs**.
4. Open each pack, copy the prompt into Flow, make the 8-second clip, post on TikTok and tag the product.
5. A few days later, type in the views and sales. Later research learns from them.

By default one pack is one prompt and one clip, which is the whole video, and the prompt tells Flow to speak the voiceover inside the clip (switch this off in Settings to get silent clips and add the voice in TikTok instead). For longer videos made of several clips, raise **Prompts per video** in Settings.

## What keeps it honest

- Every product and every fact must point to a real web page that was read during the research. Anything without a source is removed in code before it reaches the page (`app/pipeline.py`, `clean_products` and `clean_pack`).
- Each fact shows its source link so it can be checked in one tap.
- If too little real data is found for a product, the pack is marked "Not enough real data" instead of being written anyway.
- The script may not claim she personally used the product, and may not invent prices, discounts or reviews.
- `#ad` is always added to the hashtags, and every pack ends with a checklist: tag the product, turn on the AI-generated label, disclose the commission, check claims against the product page.

## Install on the server

Needs a Linux server (Ubuntu or Oracle Linux, ARM or x86) with Python 3.10 or newer.

```
git clone https://github.com/mahmoor786/tiktok trendpack
cd trendpack
bash setup.sh
```

The script installs the packages, creates a random password, opens the port on the server's firewall and installs a service that restarts on reboot. At the end it prints the address and the password.

**Oracle Cloud only:** also add an Ingress Rule for TCP port 8080 in the console (Networking > Virtual Cloud Networks > your VCN > Security Lists > Default Security List > Add Ingress Rules, source `0.0.0.0/0`, destination port `8080`).

### The two free keys

Sign in, open **Settings**, paste both keys and tap **Save keys and check connections**.

| Key | Where | What it does | Free allowance |
| --- | --- | --- | --- |
| Gemini | aistudio.google.com | Reads the research, picks products, writes the packs | Rate-limited per day; the app falls back to the lighter model when the main one runs out |
| Tavily | tavily.com | Searches the web and reads the pages | 1,000 credits a month, no card |

A session (one search plus three packs) uses about 17 Tavily credits and 4 Gemini requests.

Gemini's own Google Search tool only works on keys with billing turned on. If the key has it, the connection check finds that out and the app can use it; otherwise Tavily does the searching.

## Day-to-day commands

```
sudo systemctl status trendpack      # is it running?
sudo journalctl -u trendpack -n 50   # recent errors
sudo systemctl restart trendpack     # restart
git pull && bash setup.sh            # update to a newer version
```

Password and port live in `.env`. Keys, packs and results live in `data/trendpack.db`. Back that file up to keep her history.

## HTTPS (recommended)

Out of the box the page is served over plain `http://`, which means the password travels unencrypted. For regular use put it behind HTTPS. The simplest way with a domain or subdomain pointed at the server:

```
sudo apt install -y caddy
echo 'packs.example.com { reverse_proxy 127.0.0.1:8080 }' | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Open ports 80 and 443 instead of 8080 (server firewall and Oracle ingress rules). Caddy fetches the certificate by itself.

## Try it without keys

```
TRENDPACK_DEMO=1 APP_PASSWORD=demo ./run.sh
```

Demo mode returns made-up sample data, labelled DEMO everywhere, so the whole flow can be clicked through.

## Tests

```
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests -q
```

The tests run the full flow against fake Gemini and Tavily servers.

## How it is built

| File | Job |
| --- | --- |
| `app/main.py` | Web server, login, JSON API |
| `app/work.py` | Creates rows, queues jobs, daily automatic search |
| `app/jobs.py` | One background worker, so jobs survive a closed page and never overlap |
| `app/pipeline.py` | Find products, build packs, connection check, source enforcement |
| `app/search.py` | Tavily and Gemini Google Search, numbered sources |
| `app/llm.py` | Gemini REST client: retries, daily-limit fallback, model auto-pick |
| `app/fetch.py` | Reads a pasted product page |
| `app/prompts.py` | All wording sent to the model. Tune the video style here |
| `web/` | The page: plain HTML, CSS and JavaScript, no build step |

## Known limits

- It cannot read TikTok's private data. There is no public API for TikTok Shop sales or the affiliate marketplace, so "what is selling" comes from what is published on the open web (best-seller round-ups, trend trackers, press, shop pages). She should still confirm a product is in the UK affiliate marketplace before making the video.
- Many shop pages block servers. When a pasted link cannot be read, the app asks for the product name instead.
- Product cards have no pictures. Each has a "Look it up on TikTok" link.
- It does not post to TikTok or drive Flow. Both are done by hand on purpose: Flow has no public API, and fully automated posting gets accounts flagged.
- Views and sales are typed in by hand.
