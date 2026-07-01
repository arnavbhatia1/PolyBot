"""Standalone READ-ONLY high-frequency late-window collector.

Places NO orders, uses NO keys, holds NO single-instance lock — it only opens its
own market-data WS connections (Coinbase, Binance aggTrade, Polymarket CLOB) and
the public Gamma REST. It is NOT the trading bot; it runs alongside one safely.

Why it exists: testing the late-window edges (and several others) needs SUB-SECOND
Coinbase/Binance/CLOB-book data in the final seconds of each BTC 5-min window — which
the 1 Hz historical corpus lacks. This collects it densely so many edge hypotheses
can be tested on ONE rich dataset, far faster than waiting days for the 1 Hz recorder.

Captures, per BTC 5-min window: ~7 Hz in the final 80s (1 Hz baseline before), the
Coinbase mid (resolution venue), Binance mid + CVD (candidate leading/flow feed),
both venues' short-horizon CVD, the strike (Gamma priceToBeat), and the top-3 CLOB
book both sides. Labels each window at resolution (Gamma finalPrice). Isolated DB.

  python scripts/collect_late_window.py        # runs until Ctrl-C / killed
Analyze with scripts/analyze_collected_edges.py once enough windows accrue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.feeds.coinbase_feed import CoinbaseFeed          # noqa: E402
from polybot.feeds.binance_trades import BinanceTradesFeed, BinanceTradeAccumulator  # noqa: E402
from polybot.feeds.binance_depth import BinanceDepthFeed      # noqa: E402
from polybot.feeds.chainlink_feed import ChainlinkFeed        # noqa: E402
from polybot.feeds.clob_ws import ClobWebSocket               # noqa: E402
from polybot.feeds.market_scanner import BTCMarketScanner     # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("late_collector")

DB_PATH = Path(__file__).resolve().parent.parent / "polybot" / "db" / "late_window_collect.db"
GAMMA = "https://gamma-api.polymarket.com"
WINDOW_S = 300.0
LATE_START = 220.0          # dense sampling starts here (final 80s)
FAST_DT = 0.15              # ~7 Hz late
SLOW_DT = 1.0               # 1 Hz baseline
LABEL_DELAY_S = 45.0        # poll Gamma this long after window close for finalPrice
LABEL_GIVEUP_S = 2400.0


def _levels(side_list):
    out = []
    for lvl in (side_list or [])[:3]:
        try:
            out.append([round(float(lvl["price"]), 4), round(float(lvl["size"]), 2)])
        except (KeyError, ValueError, TypeError):
            pass
    return out


class LateWindowCollector:
    def __init__(self):
        self.acc = BinanceTradeAccumulator(max_age_s=300)
        self.binance = BinanceTradesFeed(accumulator=self.acc,
                                         ws_url="wss://stream.binance.com:9443/ws")
        # Binance USDT-M PERP (futures) — the classic BTC price leader; spot was synced
        # with Coinbase, perp may lead where spot didn't. fstream WS is silent from here
        # (futures geo-restricted) but fapi REST works → poll price at 1 Hz.
        self.perp_rest_price = None
        self.perp_rest_ts = 0.0
        self.depth = BinanceDepthFeed()          # Binance spot book imbalance (top-20)
        self.chainlink = ChainlinkFeed()         # the live RESOLUTION feed
        self.coinbase = CoinbaseFeed(ws_url="wss://ws-feed.exchange.coinbase.com",
                                     product_id="BTC-USD")
        self.clob = ClobWebSocket(url="wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self.scanner = BTCMarketScanner()
        self.http = httpx.AsyncClient(timeout=8.0)
        self.conn = None
        self.window = None                 # {market_id, window_ts, token_up, token_down, strike}
        self.pending = {}                  # market_id -> end_ts (awaiting label)
        self.buf = []
        self.rows_written = 0
        self.windows_seen = set()
        self.last_flush = time.time()
        self.last_label = 0.0

    async def setup(self):
        import aiosqlite
        self.conn = await aiosqlite.connect(str(DB_PATH))
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS lw_samples(
            window_id TEXT, ts REAL, elapsed REAL,
            coinbase REAL, binance REAL,
            cb_cvd10 REAL, bn_cvd10 REAL, bn_cvd30 REAL,
            strike REAL,
            bid_up REAL, ask_up REAL, bid_dn REAL, ask_dn REAL,
            book_up TEXT, book_dn TEXT);
          CREATE INDEX IF NOT EXISTS ix_lw_win ON lw_samples(window_id);
          CREATE TABLE IF NOT EXISTS lw_labels(
            window_id TEXT PRIMARY KEY, resolved_up INTEGER,
            final_price REAL, price_to_beat REAL, labeled_at REAL);
        """)
        # New surfaces appended at the end (existing rows -> NULL); the analyzer filters
        # to rows where the column is non-null (post-extension data only).
        cur = await self.conn.execute("PRAGMA table_info(lw_samples)")
        have = {r[1] for r in await cur.fetchall()}
        for col in ("perp_price", "perp_cvd10", "bn_depth_imb",
                    "pm_net_up", "pm_net_dn", "chainlink_px"):
            if col not in have:
                await self.conn.execute(f"ALTER TABLE lw_samples ADD COLUMN {col} REAL")
        await self.conn.commit()
        await self.binance.start()
        await self.depth.start()
        await self.chainlink.start()
        await self.coinbase.start()
        await self.clob.start()
        asyncio.create_task(self._perp_poll())
        logger.info("collector feeds started (spot+perp-rest+depth+chainlink+coinbase+clob); DB=%s", DB_PATH)

    async def _perp_poll(self):
        """1 Hz REST poll of the Binance USDT-M perp price (fstream WS is silent here)."""
        while True:
            try:
                r = await self.http.get("https://fapi.binance.com/fapi/v1/ticker/price",
                                        params={"symbol": "BTCUSDT"})
                self.perp_rest_price = float(r.json()["price"])
                self.perp_rest_ts = time.time()
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _fetch_contract(self, slug):
        try:
            r = await self.http.get(f"{GAMMA}/events", params={"slug": slug})
            r.raise_for_status()
            data = r.json()
            if data:
                return self.scanner.parse_contract(data[0] if isinstance(data, list) else data)
        except Exception as e:
            logger.debug("gamma fetch %s failed: %s", slug, e)
        return None

    async def _discover(self, window_ts):
        slug = f"btc-updown-5m-{window_ts}"
        c = await self._fetch_contract(slug)
        if not c or not c.get("token_id_up") or not c.get("token_id_down"):
            return
        prev = self.window
        if prev and prev["window_ts"] != window_ts:
            self.pending[prev["market_id"]] = prev["window_ts"] + WINDOW_S
        meta = c.get("event_metadata") or {}
        self.window = {
            "market_id": c.get("slug", slug), "window_ts": window_ts,
            "token_up": c["token_id_up"], "token_down": c["token_id_down"],
            "strike": meta.get("price_to_beat"),
        }
        self.windows_seen.add(window_ts)
        try:
            await self.clob.subscribe([c["token_id_up"], c["token_id_down"]])
        except Exception as e:
            logger.debug("clob subscribe failed: %s", e)
        logger.info("window %s discovered (strike=%s)", slug, self.window["strike"])

    def _sample(self):
        w = self.window
        if not w:
            return
        now = time.time()
        elapsed = now - w["window_ts"]
        if elapsed < 0 or elapsed > WINDOW_S:
            return
        cb = self.coinbase.state.price if self.coinbase.state.age_seconds < 5 else None
        cb_cvd = self.coinbase.get_cvd(10.0) if cb is not None else None
        bn = bn10 = bn30 = None
        if self.acc.latest_age_s < 5:
            bn = self.acc.latest_price or None
            bn10 = self.acc.get_cvd(10.0)
            bn30 = self.acc.get_cvd(30.0)
        bu = self.clob.best_bid_ask.get(w["token_up"], {})
        bd = self.clob.best_bid_ask.get(w["token_down"], {})
        book_u = self.clob.get_book(w["token_up"]) or {}
        book_d = self.clob.get_book(w["token_down"]) or {}

        def f(d, k):
            try:
                v = float(d.get(k) or 0)
                return v if 0 < v < 1 else None
            except (TypeError, ValueError):
                return None

        # --- new surfaces (None when cold; never 0.0) ---
        perp_px = self.perp_rest_price if (now - self.perp_rest_ts) < 3.0 else None
        perp_cvd = None  # perp trade stream unavailable here; price-only via REST
        bn_depth_imb = None
        try:
            bsz = sum(float(x[1]) for x in self.depth.top_bids[:20])
            asz = sum(float(x[1]) for x in self.depth.top_asks[:20])
            if bsz + asz > 0:
                bn_depth_imb = (bsz - asz) / (bsz + asz)
        except Exception:
            pass

        def pm_net(token):
            try:
                trs = self.clob.trades_since(token, now - 5.0)
            except Exception:
                return None
            net = 0.0
            for t in trs or []:
                try:
                    sz = float(t.get("size") or 0)
                    net += sz if (t.get("side") or "").upper() == "BUY" else -sz
                except (TypeError, ValueError):
                    pass
            return net
        pm_up, pm_dn = pm_net(w["token_up"]), pm_net(w["token_down"])
        cl_px = self.chainlink.price if self.chainlink.age_seconds < 60 else None
        if not cl_px or cl_px <= 0:
            cl_px = None

        self.buf.append((
            w["market_id"], round(now, 3), round(elapsed, 2),
            cb, bn, cb_cvd, bn10, bn30, w["strike"],
            f(bu, "best_bid"), f(bu, "best_ask"), f(bd, "best_bid"), f(bd, "best_ask"),
            json.dumps({"b": _levels(book_u.get("bids")), "a": _levels(book_u.get("asks"))}),
            json.dumps({"b": _levels(book_d.get("bids")), "a": _levels(book_d.get("asks"))}),
            perp_px, perp_cvd, bn_depth_imb, pm_up, pm_dn, cl_px,
        ))

    async def _flush(self):
        if not self.buf:
            return
        rows, self.buf = self.buf, []
        try:
            await self.conn.executemany(
                "INSERT INTO lw_samples (window_id, ts, elapsed, coinbase, binance, "
                "cb_cvd10, bn_cvd10, bn_cvd30, strike, bid_up, ask_up, bid_dn, ask_dn, "
                "book_up, book_dn, perp_price, perp_cvd10, bn_depth_imb, pm_net_up, "
                "pm_net_dn, chainlink_px) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            await self.conn.commit()
            self.rows_written += len(rows)
        except Exception as e:
            logger.warning("flush failed (%d rows): %s", len(rows), e)

    async def _label_pass(self):
        now = time.time()
        for mid, end_ts in list(self.pending.items()):
            if now > end_ts + LABEL_GIVEUP_S:
                self.pending.pop(mid, None); continue
            if now < end_ts + LABEL_DELAY_S:
                continue
            c = await self._fetch_contract(mid)
            meta = (c or {}).get("event_metadata") or {}
            fp, ptb = meta.get("final_price"), meta.get("price_to_beat")
            if fp is None or ptb is None:
                continue
            self.pending.pop(mid, None)
            try:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO lw_labels VALUES (?,?,?,?,?)",
                    (mid, 1 if fp >= ptb else 0, fp, ptb, now))
                # backfill any null strike for this window's samples
                await self.conn.execute(
                    "UPDATE lw_samples SET strike=? WHERE window_id=? AND strike IS NULL",
                    (ptb, mid))
                await self.conn.commit()
                logger.info("labeled %s resolved_up=%d (fp=%.1f ptb=%.1f)",
                            mid, 1 if fp >= ptb else 0, fp, ptb)
            except Exception as e:
                logger.warning("label write failed %s: %s", mid, e)

    async def run(self):
        await self.setup()
        logger.info("collecting — final %ds of each window at ~%.0f Hz; Ctrl-C to stop",
                    int(WINDOW_S - LATE_START), 1 / FAST_DT)
        try:
            while True:
                try:
                    wts = int(time.time() // 300) * 300
                    if self.window is None or self.window["window_ts"] != wts:
                        await self._discover(wts)
                    self._sample()
                    now = time.time()
                    if now - self.last_flush >= 5.0:
                        await self._flush(); self.last_flush = now
                        logger.info("rows=%d windows=%d pending_labels=%d",
                                    self.rows_written + len(self.buf), len(self.windows_seen),
                                    len(self.pending))
                    if now - self.last_label >= 30.0 and self.pending:
                        self.last_label = now
                        await self._label_pass()
                except Exception as e:
                    logger.warning("tick failed: %s", e)
                w = self.window
                late = w is not None and LATE_START <= (time.time() - w["window_ts"]) <= WINDOW_S
                await asyncio.sleep(FAST_DT if late else SLOW_DT)
        finally:
            await self._flush()
            await self.conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(LateWindowCollector().run())
