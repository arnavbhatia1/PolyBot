# Deploying PolyBot to a free 24/7 VPS — Oracle Always Free (Stockholm)

Runbook for moving the bot off the laptop onto an always-on, **$0-forever** cloud box.
Follow the phases in order. **Do Phase 0 before you invest any real setup time** — it
decides whether an Oracle box can trade at all.

---

## What this gets you, and the tradeoffs

- **Always-on.** systemd keeps the bot alive across crashes and reboots; the laptop
  host-sleep risk disappears.
- **Free forever.** Oracle Always Free is $0 indefinitely — no 12-month clock (unlike
  AWS/Azure).
- **Region = Stockholm (Sweden), *not* Ireland.** Oracle has no Ireland region, and its
  free regions nearest Dublin (Amsterdam / London / Frankfurt) are all Polymarket-blocked.
  Stockholm is the only *legal* free Oracle region. Expect **~40 ms** to Polymarket's
  AWS eu-west-2 (London) order origin — vs ~130 ms from this host today. NOTE: the
  07-10 tick-true measurement made arrival latency the #1 EV multiplier (still-stale
  asks: 21% reachable at 0.44 s total RTT -> ~28% at ~0.35 s) — a paid Amsterdam box
  ~25-35 ms closer than Stockholm is worth re-pricing against that curve once live;
  UK regions are out regardless (geoblocked jurisdiction).
- **The risk that can sink it (Phase 0 tests it):** Oracle IP ranges have the **worst
  Cloudflare reputation** of the major clouds. Polymarket fronts order submission through
  Cloudflare, so an Oracle box is the most likely to get **403'd at order time**, which
  would make it useless regardless of latency. If Phase 0 fails, fall back to **Azure
  North Europe (Dublin)** — free ~12 months then ~$8/mo, cleaner IP reputation.

---

## Prerequisites

- Oracle Cloud signup requires a phone number and a card (verification only; Always Free
  is not charged).
- An SSH keypair on your laptop: `ssh-keygen -t ed25519`.
- Your secrets ready to paste: `DISCORD_BOT_TOKEN`; for live also `POLYMARKET_PRIVATE_KEY`
  and `POLYMARKET_FUNDER`.
- A GitHub **deploy key (write)** or a **PAT** — the nightly loop commits and pushes the
  day's records to `origin main`.

---

## Phase 0 — Go / No-Go (do this FIRST)

Spin up the smallest instance (Phase 1) *or* borrow any Stockholm box, then from it:

```bash
curl -s https://polymarket.com/api/geoblock            # expect {"blocked":false,"country":"SE",...}
curl -sI https://clob.polymarket.com/ | head -5        # MUST be HTTP/2 200 — not 403
curl -s -o /dev/null -w "ttfb=%{time_starttransfer}s\n" \
  https://clob.polymarket.com/ https://clob.polymarket.com/ https://clob.polymarket.com/   # ~0.040s warm
```

- `blocked:false` **and** a non-403 CLOB response → **proceed**.
- A **403** on `clob.polymarket.com` → the Oracle IP is burned for order submission.
  **Stop.** Rebuild on Azure North Europe (Dublin) instead. Do not continue on this box.

Once live, re-confirm auth/balance with `python scripts/verify_keys.py` (GETs only) and
the order-POST path with `python scripts/smoke_order_test.py --confirm` (Phase 3) — a
raw 200 here is necessary but not sufficient.

---

## Phase 1 — Oracle signup + instance

1. Sign up at **oracle.com/cloud/free**. During signup you choose a **Home Region** —
   pick **Sweden Central (Stockholm)**.
   > ⚠️ **The home region is permanent.** Always Free resources only live in your home
   > region; there is no changing it later without a new account. This is the single most
   > important step — do not accept the auto-suggested region.

2. Create a compute instance — two options:

   | | Shape | Free allowance | RAM | Notes |
   |---|---|---|---|---|
   | **Preferred** | `VM.Standard.A1.Flex` (Ampere **ARM**) | up to 4 OCPU / 24 GB Always Free (Oracle may cap new tenancies at 2/12 — take what it offers) | **12–24 GB** | Eliminates the RAM problem entirely — no swap, pipeline has headroom. Catch: usually "out of capacity." |
   | **Fallback** | `VM.Standard.E2.1.Micro` (AMD **x86**) | 1 OCPU / 1 GB (2 free) | **1 GB** | Always available. Tight — needs the swap file in Phase 2. |

   - **A1 out-of-capacity workaround:** retry across the region's Availability Domains, try
     at off-peak hours, or loop instance creation via the OCI CLI until it lands. It is
     worth persisting for the 12+ GB.
   - **Image:** Ubuntu 24.04 LTS (ARM build for A1, x86 for E2). Ships Python 3.12.
   - **SSH:** paste your public key.

3. **Oracle networking gotcha — inbound is blocked in *two* places:**
   - In the VCN **Security List / NSG**, add an ingress rule: **TCP 22 from your home IP**.
   - The Ubuntu image also ships restrictive `iptables` INPUT rules — manage them with
     `ufw` in Phase 2.
   - Outbound is open by default; the bot needs inbound **SSH only**.

---

## Phase 2 — Harden the box

SSH in as the default user (`ubuntu`), then:

```bash
# --- swap: REQUIRED on the 1 GB E2 micro, SKIP on the 12+ GB A1 ---
sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# --- firewall: outbound open, inbound SSH-from-your-IP only ---
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow from <YOUR_HOME_IP> to any port 22
sudo ufw --force enable
sudo apt update && sudo apt install -y fail2ban

# --- SSH: keys only ---
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# --- toolchain (Python 3.12; build tools for any source builds — matters on ARM) ---
sudo apt install -y python3.12 python3.12-venv python3-pip build-essential git
sudo apt install -y unattended-upgrades   # optional: auto security patches
```

---

## Phase 3 — Deploy the bot

```bash
# dedicated service user
sudo adduser --disabled-password --gecos "" polybot
sudo su - polybot

# clone (use the SSH deploy-key URL so the nightly push works)
git clone git@github.com:<you>/PolyBot.git PolyBot && cd PolyBot
python3.12 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
#  On ARM (A1): most wheels are aarch64-native; anything without a wheel builds from
#  source (that is why build-essential is installed). coincurve is not required.

# secrets — paste over SSH, never commit
nano polybot/config/.env      # DISCORD_BOT_TOKEN, POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER
chmod 600 polybot/config/.env

# git identity + push auth for the nightly commit
git config user.name "Arnav Bhatia" && git config user.email "abhatia@mozeus.com"
#  (deploy key already grants push; if using a PAT, configure the credential helper)

# key/balance/allowance check (GET-auth), then the definitive order-POST proof
python scripts/verify_keys.py
python scripts/smoke_order_test.py --confirm   # one unfillable $1 FOK through Cloudflare
```

Keep `settings.yaml` at **`mode: paper`** for now.

---

## Phase 4 — Install the supervisor (systemd)

The repo ships the Linux wrapper and unit — `scripts/run_polybot.sh` and
`scripts/polybot.service`. As root (adjust paths/user in the unit if you did not deploy to
`/home/polybot/PolyBot`):

```bash
chmod +x /home/polybot/PolyBot/scripts/run_polybot.sh
sudo cp /home/polybot/PolyBot/scripts/polybot.service /etc/systemd/system/polybot.service
sudo systemctl daemon-reload
sudo systemctl enable --now polybot
journalctl -u polybot -f          # watch it pull and start the bot
```

`run_polybot.sh` mirrors `run_polybot.ps1`: each cycle it pulls `origin main`, runs
`polybot.main` for a full ET day (which runs the 11:45 PM ET pipeline in-process and
then exits), commits+pushes the day's records on a clean exit, and waits until
12:01 AM ET to loop. systemd's `Restart=always` + `enable` give you always-on across
crashes and reboots.

---

## Phase 5 — Validate in paper (a few days)

Before any live capital, confirm on the box:

- [ ] `systemctl status polybot` is active; `journalctl -u polybot` shows the daily loop.
- [ ] The nightly pipeline runs **without OOM** — check `free -h` during 11:45 PM ET and
      `dmesg | grep -i oom`. On the 1 GB E2 micro this is the top risk; if it OOMs, add
      more swap.
- [ ] The daily `auto: daily pipeline update` commit lands on `origin main`.
- [ ] Auto-restart works: `sudo systemctl restart polybot` → bot comes back, resumes positions.
- [ ] Reboot survival: `sudo reboot` → service auto-starts.
- [ ] **Recalibrate paper realism to the box.** The shim samples the LIVE ledger's
      order-POST RTT distribution (`paper_trader._LATENCY_QUANTILES`; knobs are
      `paper_latency_scale` / `paper_latency_floor_s`). The Phase 0 curl (~40 ms) is a
      GET's network leg only — a real order POST from the EU will still run
      ~0.31-0.35 s because ~310 ms is Polymarket's server-side matching. Interim:
      `paper_latency_scale: 0.80` (≈0.35/0.44); definitive: re-derive the quantiles
      from the box's own live fills once they exist.

---

## Phase 6 — Cutover to live

Only after **both**: (a) Phase 5 validation passes, and (b) the live edge has cleared its
kill bar — today that is the **late-window sniper** (`analyze_late_window.py`: momentum
`t_day ≥ 2` AND `p10 > 0` over ≥ 8 clean ET days at the box's measured RTT, plus the
paper-shadow comparison — the BINDING gate is the paper-shadow's realized fills, per
CLAUDE.md §2). The base strategy's gate failed final on 2026-07-01 and never deploys;
base entries are unconditionally suppressed (there is no `sniper_only` key any more) —
the complete live recipe is `mode: live` + `late_window.sniper_enabled: true`.
**Paper-complete is not that gate.** Do not change infra and flip to live in the same move.

```bash
# on the box, as polybot
nano polybot/config/settings.yaml             # mode: live + late_window.sniper_enabled: true
python scripts/verify_keys.py                 # key + funder + balance + allowance (GETs)
python scripts/smoke_order_test.py --confirm  # order-POST path through Cloudflare
sudo systemctl restart polybot
journalctl -u polybot -f
```

---

## Appendix

**Cost.** Oracle Always Free = **$0 indefinitely** within the A1 (≤4 OCPU/24 GB) or
E2.1.Micro allowance. No trial clock. (Azure fallback: free ~12 months, then ~$8/mo.)

**Idle reclamation.** Oracle can reclaim *idle* Always Free compute (low CPU/network for 7
days). A 24/7 trading bot is never idle, so this does not apply — no keep-alive hack needed.

**403 at order time (not just Phase 0).** If orders start 403'ing later, treat it as an
ambiguous outcome (never resubmit — the double-fill guard depends on this) and re-run the
Phase 0 curl. A newly-flagged IP means fall back to Azure.

**Parity note vs the Windows wrapper.** `run_polybot.sh` is a faithful translation of
`run_polybot.ps1`, including the mid-day crash-backoff (exit != 0 before 23:30 ET →
restart in 60 s; both wrappers, since 07-10) and `git pull --rebase --autostash`.

**Security.** The live private key now lives on a cloud box: keep password auth off, the
firewall at SSH-from-your-IP only, `.env` at `chmod 600`, and never commit secrets.
