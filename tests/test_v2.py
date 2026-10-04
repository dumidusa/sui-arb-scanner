"""Tests for v0.2: swap decoding, gap lifetime, CEX lag, wallet grouping, accuracy."""
import re
import statistics

from sui_scanner.cex import analyze, dex_sui_prices
from sui_scanner.classify import DEFAULT_SWAP_PATTERN, SUI, norm_type, parse_node, classify
from sui_scanner.cluster import group_bots
from sui_scanner.gaps import collect_range, detect_gaps
from sui_scanner.prices import NATIVE_USDC, Prices
from sui_scanner.report import build_report
from sui_scanner.stages import run_bots, run_sample, window_arb_counts
from sui_scanner.stats import bootstrap_rate, rel_width
from sui_scanner.store import Store
from sui_scanner.swaps import DEX_EVENTS, Swap, decode, to_swap, type_params

from fakes import (BOT_A, BOT_B, CETUS, LIFE_CYCLE, P1, P2, SUI_T, TURBOS, USDC_T, USER,
                   FakeSource, build_chain, build_events, gas_net, node, swap_ev)

RX = re.compile(DEFAULT_SWAP_PATTERN)
SUI_N, USDC_N = norm_type(SUI_T), norm_type(USDC_T)


# ------------------------------------------------------------ decoding
def test_type_params_nested():
    t = "0x1::pool::Pool<0x2::sui::SUI, 0x9::lp::LP<0x2::sui::SUI, 0x5::x::X>, 0x3::f::F>"
    assert type_params(t) == ["0x2::sui::SUI", "0x9::lp::LP<0x2::sui::SUI, 0x5::x::X>", "0x3::f::F"]


def test_decode_each_dex():
    pc = {norm_type(P1)[:66] if False else P1: (SUI_N, USDC_N)}
    from sui_scanner.classify import norm_addr
    pc = {norm_addr(P1): (SUI_N, USDC_N)}
    # cetus b->a
    d = decode("cetus", {"pool": P1, "atob": False, "amount_in": "3000000", "amount_out": "1000000000"}, pc)
    assert d == (norm_addr(P1), USDC_N, SUI_N, 3_000_000, 1_000_000_000)
    # turbos a->b with amount_a/amount_b
    d = decode("turbos", {"pool": P1, "a_to_b": True, "amount_a": "5", "amount_b": "7"}, pc)
    assert d[1:] == (SUI_N, USDC_N, 5, 7)
    # kriya/flowx clmm y->x
    d = decode("kriya_clmm", {"pool_id": P1, "x_for_y": False, "amount_x": "9", "amount_y": "4"}, pc)
    assert d[1:] == (USDC_N, SUI_N, 4, 9)
    # aftermath: coin types without 0x
    j = {"pool_id": P2, "types_in": [SUI_T[2:]], "types_out": [USDC_T[2:]],
         "amounts_in": ["10"], "amounts_out": ["20"]}
    d = decode("aftermath", j, {})
    assert d[1:] == (SUI_N, USDC_N, 10, 20)
    # flowx amm y in
    j = {"coin_x": SUI_T[2:], "coin_y": USDC_T[2:], "amount_x_in": "0", "amount_x_out": "8",
         "amount_y_in": "3", "amount_y_out": "0"}
    assert decode("flowx_amm", j, {})[1:] == (USDC_N, SUI_N, 3, 8)
    # bluemove x in
    j = {"pool_id": P2, "token_x_in": SUI_T[2:], "token_y_out": USDC_T[2:], "token_y_in": "", "token_x_out": "",
         "amount_x_in": "6", "amount_y_out": "2", "amount_y_in": "0", "amount_x_out": "0"}
    assert decode("bluemove", j, {})[1:] == (SUI_N, USDC_N, 6, 2)
    # unknown pool -> None, broken json -> None
    assert decode("cetus", {"pool": "0x99", "atob": True, "amount_in": "1", "amount_out": "1"}, pc) is None
    ev = {"contents": {"json": {"pool": P1}}, "transaction": {"digest": "x", "effects": {"checkpoint": {"sequenceNumber": 1}}}}
    assert to_swap("cetus", ev, pc) is None


# ------------------------------------------------------------ gaps
def sw(pool, price, digest, sender, cp):
    return Swap("cetus", pool, SUI_N, USDC_N, 10 ** 9, int(price * 10 ** 6), digest, sender, cp, None)


def test_gap_lifecycle_and_closers():
    s = [sw("B", 3.0, "u0", USER, 10),
         sw("A", 3.03, "u1", USER, 10),                                   # opens 1% gap
         sw("A", 3.0, "x", BOT_A, 13), sw("B", 3.0, "x", BOT_A, 13),      # arb closes after 3
         sw("A", 3.03, "u2", USER, 20), sw("A", 3.0, "y", BOT_B, 20), sw("B", 3.0, "y", BOT_B, 20),  # same cp
         sw("A", 3.05, "u3", USER, 30),                                   # opens, then pair goes quiet
         sw("C", 7.0, "z", USER, 31)]                                      # unrelated pool (other price ok)
    s[-1] = Swap("cetus", "C", SUI_N, "0xother", 10**9, 7, "z", USER, 31, None)
    r = detect_gaps(s, 0.006, stale_cps=50, bots={BOT_B})
    closed = [g for g in r.gaps if g.end == "closed"]
    assert len(closed) == 1 and closed[0].close_cp - closed[0].open_cp == 3 and closed[0].closer == "arb"
    assert r.same_cp_arbs == 1
    assert any(g.end == "censored" for g in r.gaps)  # gap at cp 30 never closed (window ended)
    r2 = detect_gaps(s + [sw("D", 1.0, "q", USER, 100)], 0.006, stale_cps=50, bots=set())
    assert any(g.end == "expired" for g in r2.gaps)  # A, B went stale -> expired


def test_gap_expires_when_stale():
    s = [sw("A", 3.0, "a", USER, 1), sw("B", 3.1, "b", USER, 1)]
    s += [sw("A", 3.0, f"k{i}", USER, 1 + i) for i in range(1, 30)]  # B goes stale
    r = detect_gaps(s, 0.006, stale_cps=10, bots=set())
    assert [g.end for g in r.gaps] == ["expired"]


def test_gap_closed_by_user_and_known_bot():
    s = [sw("A", 3.0, "a", USER, 1), sw("B", 3.1, "b", USER, 1), sw("B", 3.0, "c", BOT_B, 5)]
    r = detect_gaps(s, 0.006, stale_cps=100, bots={BOT_B})
    assert r.gaps[0].closer == "bot"
    r = detect_gaps(s, 0.006, stale_cps=100, bots=set())
    assert r.gaps[0].closer == "user"


# ------------------------------------------------------------ CEX
def test_cex_lag_and_episodes():
    cex = {t * 1000: (3.0 if t < 100 else 3.03) for t in range(0, 300)}
    dex = []
    for t in range(0, 300, 2):
        px = 3.0 if t < 107 else 3.03          # DEX follows 7 s late
        dex.append((t * 1000 + 300, px, "P"))
    a = analyze(dex, cex, threshold=0.004)
    assert a["cex_moves"] == 1 and abs(a["lag_median_s"] - 8.3) < 0.01  # first DEX print after 107 s is 108.3 s
    assert a["episodes"] == 1 and 6 <= a["episode_median_s"] <= 8
    assert a["median_abs_dev"] == 0


def test_dex_sui_prices_both_directions():
    a = Swap("cetus", "P", SUI_N, NATIVE_USDC, 2 * 10**9, 6 * 10**6, "d", None, 1, "2026-09-20T00:00:01Z")
    b = Swap("cetus", "P", NATIVE_USDC, SUI_N, 3 * 10**6, 10**9, "d", None, 1, "2026-09-20T00:00:02Z")
    pts = dex_sui_prices([a, b], {NATIVE_USDC})
    assert [round(p, 6) for _, p, _ in pts] == [3.0, 3.0]


# ------------------------------------------------------------ grouping
def _tx(digest, sender, cp, calls=(), sweep_to=None, arb=False):
    g = gas_net()
    ch = [(sender, SUI_T, -g)]
    if arb:
        ch.append((sender, USDC_T, 100))
    if sweep_to:
        ch.append((sender, USDC_T, -50))
        ch.append((sweep_to, USDC_T, 50))
    n = node(digest, sender, cp, changes=ch, events=[swap_ev(CETUS), swap_ev(TURBOS)] if arb else [])
    n["kind"] = {"commands": {"nodes": [{"function": {"fullyQualifiedName": f"{c}::m::f"}} for c in calls]}}
    return n


def test_grouping_private_package_and_sweeps(tmp_path):
    st = Store(str(tmp_path / "g.db"))
    W1, W2, W3, W4, W5 = ("0x" + c * 64 for c in "12345")
    PRIV = "0x" + "a" * 64
    PUB = "0x" + "b" * 64
    SAFE = "0x" + "d" * 64
    EXCH = "0x" + "f" * 64
    rows = [
        _tx("t1", W1, 1, [PRIV, CETUS], arb=True), _tx("t2", W2, 2, [PRIV], arb=True),   # same private pkg
        _tx("t3", W3, 3, [PUB], arb=True), _tx("t4", USER, 4, [PUB]),                     # public pkg: no link
        _tx("t5", W4, 5, [], arb=True), _tx("t6", W4, 6, [], sweep_to=SAFE),              # W4,W5 sweep to SAFE
        _tx("t7", W5, 7, [], arb=True), _tx("t8", W5, 8, [], sweep_to=SAFE),
        _tx("t9", W3, 9, [], sweep_to=EXCH), _tx("t10", USER, 10, [], sweep_to=EXCH),     # exchange: no link
        _tx("t11", W1, 11, [], sweep_to=EXCH),
    ]
    st.put_txs("sample", [(t, classify(t)) for t in (parse_node(n, RX) for n in rows)])
    bots = {W1, W2, W3, W4, W5}
    op, links = group_bots(st, bots)
    assert op[W1] == op[W2] and op[W4] == op[W5]
    assert op[W3] != op[W1] and op[W3] != op[W4] and op[W1] != op[W4]
    assert {k for _, _, k, _ in links} == {"private package", "sweep"}
    op2, _ = group_bots(st, bots, public_packages=[PRIV])
    assert op2[W1] != op2[W2]


# ------------------------------------------------------------ accuracy
def test_bootstrap_wider_for_bursty():
    secs = [30.0] * 24
    even = [10.0] * 24
    bursty = [0.0] * 23 + [240.0]
    p1, lo1, hi1 = bootstrap_rate(even, secs)
    p2, lo2, hi2 = bootstrap_rate(bursty, secs)
    assert p1 == p2
    assert rel_width(p1, lo1, hi1) < 0.01 < 0.5 < rel_width(p2, lo2, hi2)


def test_adaptive_sampling_adds_windows(tmp_path):
    src = FakeSource(build_chain())
    st = Store(str(tmp_path / "a.db"))
    run_sample(src, st, 0.5, 6, 30, 3, RX, target=0.05, max_windows=30)
    n = len(st.windows())
    assert 6 < n <= 30
    v, s = window_arb_counts(st)
    assert len(v) == n


# ------------------------------------------------------------ end to end (fake source)
def test_gaps_end_to_end(tmp_path):
    src = FakeSource(build_chain(), events=build_events())
    st = Store(str(tmp_path / "e.db"))
    run_sample(src, st, 0.5, 10, 300, 3, RX)
    run_bots(src, st, 3, 100_000, 2, RX)
    from sui_scanner.gaps import run_gaps
    run_gaps(src, st, 6, 2400, 2, cex=None)
    md = build_report(st, Prices(3.0), src)
    print(md)
    assert "## 4. Gap lifetime" in md and "Who closed them: arb txs **100%**" in md
    # lifetimes 2,3,10,50 + same-checkpoint fixes; median of {2,3,10,50,...} must be 3 or 10
    swaps = []
    for w in st.gap_windows():
        swaps += st.swaps(w["id"])
    assert len(swaps) > 0
    r = detect_gaps(swaps, 0.006, 2400, set())
    lives = sorted({g.close_cp - g.open_cp for g in r.gaps if g.end == "closed"})
    assert lives == [2, 3, 10, 50]
    assert r.same_cp_arbs > 0
    assert "Operators (wallets grouped)" in md


def test_live_mode_follows_head(tmp_path):
    from sui_scanner.gaps import run_live

    class Moving(FakeSource):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.head = 20_000

        def latest_checkpoint(self):
            from fakes import cp_ts
            self.head += 400
            return self.head, cp_ts(self.head)

    src = Moving(build_chain(), events=build_events())
    st = Store(str(tmp_path / "l.db"))
    run_sample(src, st, 0.2, 6, 120, 2, RX)
    run_live(src, st, minutes=0.02, poll=0.05)
    live = [w for w in st.gap_windows() if w["mode"] == "live"]
    assert len(live) == 1 and live[0]["complete"] == 1 and live[0]["end_cp"] > live[0]["start_cp"] + 1000
    assert len(st.swaps(live[0]["id"])) > 10
    md = build_report(st, Prices(3.0), src)
    assert "## 4. Gap lifetime" in md


def test_real_world_outliers_are_left_out(tmp_path):
    """v0.1 mainnet run: single user txs looked like -$15K/day 'arb losses'."""
    from sui_scanner.classify import parse_node, classify
    src = FakeSource(build_chain())
    st = Store(str(tmp_path / "o.db"))
    run_sample(src, st, 0.5, 10, 600, 3, RX)
    w = st.windows()[0]
    G = gas_net()
    whale = "0x" + "9" * 64
    big = node("bigloss", whale, w["start_cp"] + 1, changes=[(whale, SUI_T, -100 * 10**9 - G)],
               events=[swap_ev(CETUS), swap_ev(TURBOS)])          # 100 SUI gone into a protocol
    sent = node("sentaway", whale, w["start_cp"] + 2,
                changes=[(whale, SUI_T, -10**9 - G), ("0x" + "8" * 64, USDC_T, 3_000_000)],
                events=[swap_ev(CETUS), swap_ev(TURBOS)])         # bought USDC for someone else
    rows = [(t, classify(t)) for t in (parse_node(n, RX) for n in (big, sent))]
    assert rows[1][1].kind == "other"
    st.put_txs("sample", rows)
    md = build_report(st, Prices(3.0), src)
    assert "One-off wallets left out" in md and whale[:6] not in md.split("## 2.")[1].split("## 3.")[0]
    md2 = build_report(st, Prices(3.0), src, min_attempts=1)
    assert "Left-out big txs" in md2 and "bigloss" in md2
