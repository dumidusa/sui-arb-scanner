"""Build the decision report from the database."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict

from .cex import analyze, default_stables, dex_sui_prices, iso_ms
from .classify import SUI
from .cluster import group_bots
from .gaps import detect_gaps
from .prices import Prices
from .stats import bootstrap_rate, rel_width
from .stages import ts_to_dt
from .store import Store


def short(a: str | None) -> str:
    if not a:
        return "?"
    return f"{a[:6]}…{a[-4:]}"


def explorer(a: str) -> str:
    return f"https://suiscan.xyz/mainnet/account/{a}"


def money(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}${x/1e6:.2f}M"
    if x >= 1_000:
        return f"{sign}${x/1e3:.1f}K"
    return f"{sign}${x:.2f}"


class Valuer:
    def __init__(self, store: Store, prices: Prices, src=None):
        self.store, self.prices, self.src = store, prices, src
        self.unpriced = Counter()  # coin -> txs

    def decimals(self, coin: str) -> tuple[int | None, str | None]:
        row = self.store.coin(coin)
        if row:
            return row[0], row[1]
        if coin == SUI:
            return 9, "SUI"
        if self.src is None:
            return None, None
        dec, sym = self.src.coin_meta(coin)
        self.store.put_coin(coin, dec, sym)
        return dec, sym

    def net_usd(self, gross: dict[str, int], gas_net: int, count: bool = True) -> float | None:
        """None = cannot value (unpriced coin or no SUI price)."""
        if not self.prices.sui_usd:
            return None
        total = -gas_net / 1e9 * self.prices.sui_usd
        ok = True
        for coin, amt in gross.items():
            p = self.prices.usd(coin)
            dec, _ = self.decimals(coin)
            if p is None or dec is None:
                if count:
                    self.unpriced[coin] += 1
                ok = False
                continue
            total += amt / 10 ** dec * p
        return total if ok else None

    def gas_usd(self, gas_net: int) -> float:
        return gas_net / 1e9 * (self.prices.sui_usd or 0.0)


def tx_link(d: str) -> str:
    return f"https://suiscan.xyz/mainnet/tx/{d}"


def clean_rows(rows, val: "Valuer", max_tx_usd: float):
    """rows: (sender, kind, gross, gas, venues, checkpoint, digest, recipients).

    Real-data fixes (v0.3):
      * arb_loss where someone ELSE received coins -> 'other' (user bought and sent away)
      * arb / arb_loss worth more than max_tx_usd in one tx -> 'outlier' (shown for manual
        check, not counted): usually a user withdrawing from a protocol and swapping,
        or buying something whose output goes into an object.
    """
    out, outliers = [], []
    for r in rows:
        sender, kind, gross_s, gas, venues_s, cp, digest, recips = r
        if kind == "arb_loss" and recips and json.loads(recips):
            kind = "other"
        if kind in ("arb", "arb_loss") and max_tx_usd:
            u = val.net_usd(json.loads(gross_s), gas, count=False)
            if u is not None and abs(u) > max_tx_usd:
                outliers.append((sender, kind, u, digest))
                kind = "outlier"
        out.append((sender, kind, gross_s, gas, venues_s, cp, digest))
    return out, outliers


def _bot_stats(rows, bots: set[str], val: Valuer) -> dict[str, dict]:
    st: dict[str, dict] = defaultdict(lambda: dict(wins=0, losses=0, fails=0, unpriced=0,
                                                   net_usd=0.0, gas_usd=0.0, venues=Counter(),
                                                   profit_coins=Counter()))
    for sender, kind, gross_s, gas, venues_s in rows:
        if sender not in bots:
            continue
        s = st[sender]
        s["gas_usd"] += val.gas_usd(gas)
        if kind == "failed":
            s["fails"] += 1
            s["net_usd"] -= val.gas_usd(gas)
            continue
        if kind not in ("arb", "arb_loss"):
            continue
        s["wins" if kind == "arb" else "losses"] += 1
        gross = json.loads(gross_s)
        for c in gross:
            s["profit_coins"][c] += 1
        for v in json.loads(venues_s):
            s["venues"][v] += 1
        u = val.net_usd(gross, gas)
        if u is None:
            s["unpriced"] += 1
        else:
            s["net_usd"] += u
    return st


def coin_name(val, c: str) -> str:
    sym = val.decimals(c)[1] if val.src is not None or val.store.coin(c) else None
    return sym or c.split("<")[0].split("::")[-1]


def build_report(store: Store, prices: Prices, src=None, csv_path: str | None = None,
                 gap_threshold: float = 0.006, stale_cps: int = 2400,
                 cex_threshold: float = 0.004, stables: set[str] | None = None,
                 public_packages=(), max_tx_usd: float = 100.0, min_attempts: int = 3) -> str:
    plan = store.get_meta("plan")
    if not plan:
        return "No data yet. Run `sample` first."
    val = Valuer(store, prices, src)
    L: list[str] = []
    P = L.append
    cps_s = plan["cps_per_sec"]

    # ---------------- sample coverage
    wins_ = [w for w in store.windows() if w["complete"] and w["start_ts"] and w["end_ts"]]
    total_w = len(store.windows())
    per_cp = 1.0 / cps_s
    win_secs = {w["id"]: (ts_to_dt(w["end_ts"]) - ts_to_dt(w["start_ts"])).total_seconds() + per_cp
                for w in wins_}
    sampled = sum(win_secs.values())
    if sampled <= 0:
        return "No complete windows yet. Run `sample` again."
    day = 86_400 / sampled  # multiply sample totals by this to get per-day

    ranges = [(w["start_cp"], w["end_cp"], w["id"]) for w in wins_]

    def win_of(cp):
        for s_, e_, i in ranges:
            if s_ <= cp <= e_:
                return i
        return None

    rows = store.query("SELECT sender, kind, gross, gas_net, venues, checkpoint, digest, recipients "
                       "FROM txs WHERE stage='sample'")
    rows = [r for r in rows if win_of(r[5]) is not None]
    n_tx = len(rows)
    rows, outliers = clean_rows(rows, val, max_tx_usd)
    cand = {r[0] for r in rows if r[1] in ("arb", "arb_loss") and r[0]}
    attempts = Counter(r[0] for r in rows if r[0] in cand and r[1] in ("arb", "arb_loss", "failed"))
    bots = {b for b in cand if attempts[b] >= min_attempts}
    one_off = cand - bots
    st = _bot_stats([r[:5] for r in rows], bots, val)

    P("# Sui arbitrage scanner report\n")
    P(f"- Range: last **{plan['days']} days**, {len(wins_)}/{total_w} windows complete, "
      f"**{sampled/3600:.2f} h** of chain time sampled")
    P(f"- Transactions scanned: **{n_tx:,}** (≈ {n_tx*day:,.0f} programmable txs/day)")
    P(f"- SUI price used: {money(prices.sui_usd) if prices.sui_usd else 'none (USD values off)'}\n")

    tot_w = sum(s["wins"] for s in st.values())
    tot_l = sum(s["losses"] for s in st.values())
    tot_f = sum(s["fails"] for s in st.values())
    tot_net = sum(s["net_usd"] for s in st.values())
    tot_unp = sum(s["unpriced"] for s in st.values())

    # per-window net -> bootstrap range
    per_win = defaultdict(float)
    for sender, kind, gross_s, gas, _v, cp, _d in rows:
        if sender in bots and kind in ("arb", "arb_loss", "failed"):
            if kind == "failed":
                per_win[win_of(cp)] -= val.gas_usd(gas)
            else:
                u = val.net_usd(json.loads(gross_s), gas, count=False)
                per_win[win_of(cp)] += u or 0.0
    ids = list(win_secs)
    pie, pie_lo, pie_hi = bootstrap_rate([per_win.get(i, 0.0) for i in ids], [win_secs[i] for i in ids])
    rw = rel_width(pie, pie_lo, pie_hi)

    P("## 1. The pie (estimated per day)\n")
    P("| Metric | In sample | Per day (est.) |")
    P("|---|---|---|")
    P(f"| Arb bots counted (wallets with ≥{min_attempts} attempts) | {len(bots)} | – |")
    P(f"| One-off wallets left out (< {min_attempts} attempts, mostly not bots) | {len(one_off)} | – |")
    P(f"| Single txs worth > {money(max_tx_usd)} left out (see list) | {len(outliers)} | – |")
    P(f"| Winning arb txs | {tot_w:,} | {tot_w*day:,.0f} |")
    P(f"| Losing arb txs | {tot_l:,} | {tot_l*day:,.0f} |")
    P(f"| Failed txs by bots | {tot_f:,} | {tot_f*day:,.0f} |")
    P(f"| **Net profit, all bots** (priced only) | {money(tot_net)} | **{money(pie)}** |")
    if prices.sui_usd:
        P(f"| Likely range (80%) | – | {money(pie_lo)} – {money(pie_hi)} |")
    unp_share = tot_unp / max(1, tot_w + tot_l)
    P(f"| Arb txs not valued (unpriced coin) | {tot_unp:,} ({unp_share:.0%}) | – |\n")
    if prices.sui_usd and len(ids) >= 2:
        tag = "good" if rw <= 0.25 else ("ok" if rw <= 0.5 else "noisy → run `sample --target-precision 0.25`")
        P(f"Precision: ±{rw:.0%} ({tag}). Range comes from re-sampling the windows (bootstrap).\n")

    if outliers:
        P(f"**Left-out big txs** (one tx > {money(max_tx_usd)} — usually a user, not an arb; "
          f"check on the explorer, change with `--max-tx-usd`):\n")
        P("| Tx | Wallet | Label | Value |")
        P("|---|---|---|---|")
        for snd, kind, u, d in sorted(outliers, key=lambda o: -abs(o[2]))[:10]:
            P(f"| [{d[:8]}…]({tx_link(d)}) | {short(snd)} | {kind} | {money(u)} |")
        P("")

    if val.unpriced:
        P("**Profit coins without a price** (add with `--price TYPE=USD`, then re-run `report`):\n")
        P("| Coin | Symbol | Arb txs |")
        P("|---|---|---|")
        for coin, c in val.unpriced.most_common(8):
            _, sym = val.decimals(coin)
            P(f"| `{coin[:20]}…{coin.split('::',1)[-1]}` | {sym or '?'} | {c} |")
        P("")

    # ---------------- competition: wallets, then operators
    pos = sorted((s["net_usd"] for s in st.values() if s["net_usd"] > 0), reverse=True)
    pos_sum = sum(pos)
    P("## 2. Competition\n")
    P("### Wallets\n")
    P("| # | Bot | Wins | Losses | Fails | Fail rate | Net/day | Share | Unpriced |")
    P("|---|---|---|---|---|---|---|---|---|")
    ranked = sorted(st.items(), key=lambda kv: (kv[1]["net_usd"], kv[1]["wins"]), reverse=True)
    for i, (b, s) in enumerate(ranked[:15], 1):
        att = s["wins"] + s["losses"] + s["fails"]
        share = s["net_usd"] / pos_sum if pos_sum and s["net_usd"] > 0 else 0.0
        P(f"| {i} | [{short(b)}]({explorer(b)}) | {s['wins']} | {s['losses']} | {s['fails']} | "
          f"{s['fails']/att:.0%} | {money(s['net_usd']*day)} | {share:.0%} | {s['unpriced']} |")
    P("")

    op_of, links = group_bots(store, bots, public_packages)
    ops = defaultdict(lambda: {"wallets": [], "net": 0.0, "wins": 0, "fails": 0})
    for b, s in st.items():
        o = ops[op_of.get(b, b)]
        o["wallets"].append(b)
        o["net"] += s["net_usd"]
        o["wins"] += s["wins"]
        o["fails"] += s["fails"]
    opos = sorted((o["net"] for o in ops.values() if o["net"] > 0), reverse=True)
    opos_sum = sum(opos)
    top1 = opos[0] / opos_sum if opos_sum else 0.0
    top3 = sum(opos[:3]) / opos_sum if opos_sum else 0.0
    P("### Operators (wallets grouped)\n")
    P(f"- **{len(bots)} wallets → {len(ops)} operators** "
      f"({len(links)} link{'s' if len(links) != 1 else ''} found)")
    P(f"- Top-1 operator share: **{top1:.0%}**, top-3: **{top3:.0%}**, "
      f"operators with ≥5% share: **{sum(1 for x in opos if opos_sum and x/opos_sum >= 0.05)}**\n")
    P("| # | Operator | Wallets | Wins | Fails | Net/day | Share |")
    P("|---|---|---|---|---|---|---|")
    oranked = sorted(ops.values(), key=lambda o: (o["net"], o["wins"]), reverse=True)
    for i, o in enumerate(oranked[:10], 1):
        lead = sorted(o["wallets"], key=lambda w: -st[w]["net_usd"])[0]
        share = o["net"] / opos_sum if opos_sum and o["net"] > 0 else 0.0
        P(f"| {i} | {short(lead)}{' +' + str(len(o['wallets'])-1) if len(o['wallets']) > 1 else ''} | "
          f"{len(o['wallets'])} | {o['wins']} | {o['fails']} | {money(o['net']*day)} | {share:.0%} |")
    if links:
        P("\n<details><summary>Why wallets were grouped (check on an explorer)</summary>\n")
        for a, b, kind, via in links[:30]:
            P(f"- {short(a)} ↔ {short(b)} — {kind} `{short(via)}`")
        P("\n</details>")
    P("")

    venues = Counter()
    coins = Counter()
    for s in st.values():
        venues.update(s["venues"])
        coins.update(s["profit_coins"])
    if venues:
        P("**Venues bots use** (event module in winning/losing arbs — look up the package on an explorer):\n")
        P("| Package::module | Arb txs |")
        P("|---|---|")
        for v, c in venues.most_common(10):
            pkg, mod = v.split("::", 1)
            P(f"| `{short(pkg)}::{mod}` | {c} |")
        P("")
    if coins:
        P("**Coins profit is taken in:** " + ", ".join(
            f"{(val.decimals(c)[1] or c.split('::')[-1])} ({n})" for c, n in coins.most_common(6)) + "\n")

    # ---------------- deep dive
    deep = store.query("SELECT address, txs, first_ts, last_ts, capped FROM deep WHERE done=1")
    if deep:
        P("## 3. Bot deep dive (their own full history)\n")
        P("| Bot | Txs fetched | Span (days) | Txs/day | Wins/day | Fail rate | Net/day | Capped |")
        P("|---|---|---|---|---|---|---|---|")
        drows = store.query("SELECT sender, kind, gross, gas_net, venues, checkpoint, digest, recipients "
                            "FROM txs WHERE stage='deep'")
        drows, _ = clean_rows(drows, val, max_tx_usd)
        dbots = {d[0] for d in deep}
        dst = _bot_stats([r[:5] for r in drows], dbots, val)
        latest = ts_to_dt(plan["latest_ts"])
        sum_day = 0.0
        for addr, n, first_ts, last_ts, capped in sorted(deep, key=lambda d: -dst[d[0]]["net_usd"]):
            if capped and first_ts:
                span = max((latest - ts_to_dt(first_ts)).total_seconds(), 60) / 86_400
            else:
                span = plan["days"]
            s = dst[addr]
            att = s["wins"] + s["losses"] + s["fails"]
            nd = s["net_usd"] / span
            sum_day += nd
            P(f"| [{short(addr)}]({explorer(addr)}) | {n:,} | {span:.2f} | {n/span:,.0f} | "
              f"{s['wins']/span:,.0f} | {s['fails']/max(1,att):.0%} | {money(nd)} | {'yes' if capped else 'no'} |")
        P(f"\nSum of deep-dived bots: **{money(sum_day)}/day** (a solid lower bound for the pie).\n")
        P("Capped = only the most recent txs were read; rates use that shorter span.\n")

    # ---------------- gap lifetime
    gwins = [w for w in store.gap_windows()
             if w["complete"] and w["start_ts"] and w["end_ts"] and w["end_cp"] >= w["start_cp"]]
    gap_summary = None
    cex_summary = None
    if gwins:
        all_gaps, same_cp, gsecs = [], 0, 0.0
        cex_parts = []
        stables = stables or default_stables()
        for w in gwins:
            sw = store.swaps(w["id"])
            r = detect_gaps(sw, gap_threshold, stale_cps, bots, w["start_cp"], w["end_cp"])
            all_gaps += r.gaps
            same_cp += r.same_cp_arbs
            secs = (ts_to_dt(w["end_ts"]) - ts_to_dt(w["start_ts"])).total_seconds() + per_cp
            gsecs += secs
            dexp = dex_sui_prices(sw, stables)
            if dexp:
                cx = store.cex_range(iso_ms(w["start_ts"]) - 5000, iso_ms(w["end_ts"]) + 5000)
                a = analyze(dexp, cx, cex_threshold)
                if a:
                    cex_parts.append((a, secs))
        gday = 86_400 / gsecs if gsecs else 0
        closed = [g for g in all_gaps if g.end == "closed"]
        lives = [g.close_cp - g.open_cp for g in closed]
        P("## 4. Gap lifetime\n")
        P(f"Spread between pools of the same pair > **{gap_threshold:.2%}** = gap. "
          f"{len(gwins)} windows, {gsecs/3600:.2f} h of chain time.\n")
        P("| Metric | In sample | Per day (est.) |")
        P("|---|---|---|")
        P(f"| Gaps opened (visible at checkpoint end) | {len(all_gaps):,} | {len(all_gaps)*gday:,.0f} |")
        P(f"| Fixed inside the same checkpoint (arb landed with the move) | {same_cp:,} | {same_cp*gday:,.0f} |")
        P(f"| Closed later | {len(closed):,} | {len(closed)*gday:,.0f} |")
        exp = sum(1 for g in all_gaps if g.end == "expired")
        cen = sum(1 for g in all_gaps if g.end == "censored")
        P(f"| Expired (pair went quiet, nobody fixed it) | {exp:,} | {exp*gday:,.0f} |")
        P(f"| Still open at window end | {cen:,} | – |\n")
        if lives:
            buckets = [(1, 1), (2, 4), (5, 20), (21, 100), (101, 10**12)]
            P("| Lifetime | Checkpoints | ≈ Seconds | Gaps | Share |")
            P("|---|---|---|---|---|")
            for lo_, hi_ in buckets:
                n = sum(1 for x in lives if lo_ <= x <= hi_)
                lab = f"{lo_}" if lo_ == hi_ else (f"{lo_}–{hi_}" if hi_ < 10**12 else f">{lo_-1}")
                secs_lab = (f"{lo_/cps_s:.1f}" if lo_ == hi_ else
                            (f"{lo_/cps_s:.1f}–{hi_/cps_s:.0f}" if hi_ < 10**12 else f">{(lo_-1)/cps_s:.0f}"))
                P(f"| {'fast' if hi_ <= 4 else ('medium' if hi_ <= 20 else 'slow')} | {lab} | {secs_lab} | "
                  f"{n} | {n/len(lives):.0%} |")
            cl = Counter(g.closer for g in closed)
            P(f"\nWho closed them: arb txs **{cl['arb']/len(closed):.0%}**, known bots "
              f"**{cl['bot']/len(closed):.0%}**, normal users **{cl['user']/len(closed):.0%}**. "
              f"Median life **{statistics.median(lives)} checkpoints** (≈ {statistics.median(lives)/cps_s:.1f} s).\n")
            per_pair = defaultdict(list)
            for g in closed:
                per_pair[g.pair].append(g)
            slow = sorted(((p, gs) for p, gs in per_pair.items() if len(gs) >= 3),
                          key=lambda kv: -statistics.median(g.close_cp - g.open_cp for g in kv[1]))
            if slow:
                P("**Slowest pairs** (gaps stay open longest → fewer bots watching):\n")
                P("| Pair | Gaps | Median life (s) | Median peak spread | Closed by arb |")
                P("|---|---|---|---|---|")
                for p, gs in slow[:8]:
                    ml = statistics.median(g.close_cp - g.open_cp for g in gs) / cps_s
                    pk = statistics.median(g.peak for g in gs)
                    ab = sum(1 for g in gs if g.closer == "arb") / len(gs)
                    P(f"| {coin_name(val, p[0])} / {coin_name(val, p[1])} | {len(gs)} | {ml:.1f} | "
                      f"{pk:.2%} | {ab:.0%} |")
                P("")
            fast = sum(1 for x in lives if x <= 4) + same_cp
            gap_summary = {"median": statistics.median(lives), "fast_share": fast / (len(lives) + same_cp),
                           "slow_share": (sum(1 for x in lives if x > 20) + exp) / (len(lives) + exp + same_cp)}
        P("_Gap = cross-pool spread of **execution prices** (includes fees/slippage of each swap). "
          "Use it to compare speed and competition, not to price exact profit._\n")

        # ---------------- CEX vs DEX
        P("## 5. CEX ↔ DEX (Binance SUIUSDT vs Sui DEX SUI/USDC)\n")
        if not cex_parts:
            P("No overlapping data (no SUI/USDC swaps, or Binance data missing — run without `--no-cex`).\n")
        else:
            absd = sorted(x for a, _ in cex_parts for x in a["_absd"])
            durs = [x for a, _ in cex_parts for x in a["_durs"]]
            lags = [x for a, _ in cex_parts for x in a["_lags"]]
            n_ep = sum(a["episodes"] for a, _ in cex_parts)
            n_moves = sum(a["cex_moves"] for a, _ in cex_parts)
            n_nc = sum(a["lag_not_caught"] for a, _ in cex_parts)
            csecs = sum(sx for _, sx in cex_parts)
            P("| Metric | Value |")
            P("|---|---|")
            P(f"| DEX prints compared | {len(absd):,} |")
            P(f"| Median / p90 gap DEX vs CEX | {statistics.median(absd):.3%} / {absd[int(0.9*(len(absd)-1))]:.3%} |")
            P(f"| Episodes with gap > {cex_threshold:.2%} | {n_ep} (≈ {n_ep*86_400/csecs:,.0f}/day) |")
            if durs:
                P(f"| Median episode length | {statistics.median(durs):.1f} s |")
            P(f"| Fast CEX moves (≥0.2% in 5 s) | {n_moves} |")
            if lags:
                P(f"| Median DEX lag after a CEX move | **{statistics.median(lags):.1f} s** |")
            P(f"| CEX moves the DEX did not follow within 120 s | {n_nc} |\n")
            P("_Needs capital on Binance AND on Sui (no flash loans) plus a hedge. "
              "USDT vs USDC treated as 1:1._\n")
            cex_summary = {"lag": statistics.median(lags) if lags else None,
                           "ep_day": n_ep * 86_400 / csecs}

    # ---------------- verdict
    P("## 6. Decision gate\n")
    if not prices.sui_usd:
        P("No SUI price → cannot judge in USD. Re-run `report --sui-price <USD>`.")
    elif unp_share > 0.4:
        P(f"⚠️ {unp_share:.0%} of arbs are unpriced — add prices for the coins above before deciding.")
    if prices.sui_usd:
        rng = f" (likely {money(pie_lo)}–{money(pie_hi)})" if len(ids) >= 2 else ""
        if pie_lo >= 20_000 and top1 < 0.6:
            P(f"🟢 Pie ≈ {money(pie)}/day{rng}, top operator {top1:.0%}. **Continue to the full scanner.**")
        elif pie_hi < 2_000:
            P(f"🔴 Pie ≈ {money(pie)}/day{rng} — too small for the effort. **Stop or look at niches/other strategies.**")
        else:
            P(f"🟡 Pie ≈ {money(pie)}/day{rng}, top operator {top1:.0%}. **Borderline** — check the deep dive, "
              f"fail rates (high = crowded races) and the slow pairs.")
    if gap_summary:
        if gap_summary["fast_share"] > 0.8:
            P(f"- Gaps: {gap_summary['fast_share']:.0%} are fixed within ~1 s → **speed race**; "
              f"enter only via slow pairs / new venues.")
        else:
            P(f"- Gaps: only {gap_summary['fast_share']:.0%} fixed within ~1 s, "
              f"{gap_summary['slow_share']:.0%} live >5 s or expire → **room for a slower bot**.")
    if cex_summary and cex_summary["lag"] is not None:
        P(f"- CEX↔DEX: DEX follows Binance in ~{cex_summary['lag']:.0f} s, "
          f"~{cex_summary['ep_day']:,.0f} episodes/day above threshold (needs capital + hedge).")
    P("\n_All numbers are estimates from sampled windows. Arbs paid in coins with no price are not valued._")

    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["bot", "operator", "wins", "losses", "fails", "net_usd_per_day", "unpriced", "explorer"])
            for b, s in ranked:
                w.writerow([b, op_of.get(b, b), s["wins"], s["losses"], s["fails"],
                            round(s["net_usd"] * day, 2), s["unpriced"], explorer(b)])
    return "\n".join(L)
