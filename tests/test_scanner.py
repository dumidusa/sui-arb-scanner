import re

from sui_scanner.classify import DEFAULT_SWAP_PATTERN, SUI, classify, norm_type, parse_node
from sui_scanner.prices import Prices
from sui_scanner.report import build_report
from sui_scanner.stages import run_bots, run_sample
from sui_scanner.store import Store

from fakes import (BOT_A, BOT_B, CETUS, DEEP_T, SUI_T, TURBOS, USDC_T, USER, FakeSource,
                   build_chain, gas_net, node, swap_ev)

RX = re.compile(DEFAULT_SWAP_PATTERN)
G = gas_net()


def lab(n):
    return classify(parse_node(n, RX))


# ------------------------------------------------------------ classifier
def test_usdc_arb_win():
    l = lab(node("x", BOT_A, 1, changes=[(BOT_A, SUI_T, -G), (BOT_A, USDC_T, 500_000)],
                 events=[swap_ev(CETUS), swap_ev(TURBOS)]))
    assert l.kind == "arb"
    assert l.gross == {norm_type(USDC_T): 500_000}
    assert l.gas_net == G


def test_sui_arb_win_gross_excludes_gas():
    l = lab(node("x", BOT_B, 1, changes=[(BOT_B, SUI_T, 200_000_000 - G)],
                 events=[swap_ev(CETUS), swap_ev(TURBOS)]))
    assert l.kind == "arb" and l.gross == {SUI: 200_000_000}


def test_user_two_hop_swap_is_not_arb():
    l = lab(node("x", USER, 1, changes=[(USER, SUI_T, -5_000_000_000 - G), (USER, USDC_T, 17_000_000)],
                 events=[swap_ev(CETUS), swap_ev(TURBOS)]))
    assert l.kind == "other"


def test_user_sells_token_is_not_arb():
    l = lab(node("x", USER, 1, changes=[(USER, SUI_T, 900_000_000), (USER, USDC_T, -3_000_000)],
                 events=[swap_ev(CETUS), swap_ev(TURBOS)]))
    assert l.kind == "other"


def test_cyclic_loss():
    l = lab(node("x", BOT_B, 1, changes=[(BOT_B, SUI_T, -10_000_000 - G)],
                 events=[swap_ev(CETUS), swap_ev(TURBOS)]))
    assert l.kind == "arb_loss" and l.gross == {SUI: -10_000_000}


def test_single_swap_not_arb():
    l = lab(node("x", BOT_A, 1, changes=[(BOT_A, USDC_T, 5)], events=[swap_ev(CETUS)]))
    assert l.kind == "other"


def test_failed():
    l = lab(node("x", BOT_B, 1, status="FAILURE", changes=[(BOT_B, SUI_T, -G)]))
    assert l.kind == "failed"


def test_ignores_other_owners_and_short_addresses():
    n = node("x", "0x" + "0" * 60 + "abcd", 1,
             changes=[("0xabcd", USDC_T, 10), ("0x" + "9" * 64, USDC_T, -10)],
             events=[swap_ev(CETUS), swap_ev(TURBOS)])
    tx = parse_node(n, RX)
    assert tx.sender == "0x" + "0" * 60 + "abcd"
    assert tx.changes == {norm_type(USDC_T): 10}  # short form matched, stranger ignored


def test_deepbook_order_filled_counts_as_swap():
    ev = {"contents": {"type": {"repr": "0xdee9::pool::OrderFilled"}}}
    tx = parse_node(node("x", BOT_A, 1, events=[ev, swap_ev(CETUS)]), RX)
    assert tx.swap_events == 2


# ------------------------------------------------------------ end to end
def run_all(tmp_path, max_txs=100_000):
    src = FakeSource(build_chain())
    store = Store(str(tmp_path / "t.db"))
    run_sample(src, store, days=0.5, n_windows=20, window_cps=600, workers=3, swap_re=RX)
    run_bots(src, store, top=5, max_txs=max_txs, workers=2, swap_re=RX)
    return src, store


def test_end_to_end_report(tmp_path):
    src, store = run_all(tmp_path)
    md = build_report(store, Prices(3.0), src, str(tmp_path / "b.csv"))
    print(md)
    assert "Decision gate" in md
    # leaderboard order: A first
    lines = [l for l in md.splitlines() if l.startswith("| 1 |") or l.startswith("| 2 |")]
    assert BOT_A[:6] in lines[0] and BOT_B[:6] in lines[1]
    # unpriced DEEP coin is listed
    assert "DEEP" in md
    # user is never a bot
    assert USER[:6] not in md


def test_estimates_close_to_truth(tmp_path):
    src, store = run_all(tmp_path)
    from sui_scanner.report import _bot_stats, Valuer
    rows = store.query("SELECT sender, kind, gross, gas_net, venues FROM txs WHERE stage='deep'")
    st = _bot_stats(rows, {BOT_A, BOT_B}, Valuer(store, Prices(3.0), src))
    plan = store.get_meta("plan")
    days = (plan["latest_cp"] - plan["start_cp"]) / 4 / 86_400  # deep stage reads from start_cp
    # truth from generator probabilities (tx every 3 cps, 4 cps/s)
    tx_day = 4 / 3 * 86_400
    gas_usd = G / 1e9 * 3.0
    a_true = tx_day * 0.10 * (0.5 - gas_usd) - tx_day * 0.02 * gas_usd  # DEEP wins unpriced, only gas
    b_true = tx_day * (0.03 * (0.6 - gas_usd) - 0.12 * gas_usd - 0.02 * (0.03 + gas_usd))
    # deep stage reads the whole fake chain (not capped), so compare per-day over full span
    a_est = st[BOT_A]["net_usd"] / days
    b_est = st[BOT_B]["net_usd"] / days
    # A's unpriced DEEP wins are excluded from net (not even gas) -> add back for comparison
    a_est_adj = a_est - st[BOT_A]["unpriced"] * gas_usd / days
    assert abs(a_est_adj - a_true) / a_true < 0.05, (a_est_adj, a_true)
    assert abs(b_est - b_true) / b_true < 0.08, (b_est, b_true)


def test_resume_is_idempotent(tmp_path):
    src, store = run_all(tmp_path)
    n1 = store.query("SELECT COUNT(*) FROM txs")[0][0]
    calls = src.calls
    run_sample(src, store, 0.5, 20, 600, 3, RX)
    run_bots(src, store, 5, 100_000, 2, RX)
    assert store.query("SELECT COUNT(*) FROM txs")[0][0] == n1
    assert src.calls == calls  # nothing re-fetched


def test_capped_deep_dive(tmp_path):
    src, store = run_all(tmp_path, max_txs=50)
    rows = store.query("SELECT txs, capped FROM deep")
    assert rows and all(c == 1 and n >= 50 for n, c in rows)
    md = build_report(store, Prices(3.0), src)
    assert "| yes |" in md


def test_no_price_still_reports(tmp_path):
    src, store = run_all(tmp_path)
    md = build_report(store, Prices(None), src)
    assert "No SUI price" in md
