"""Run the real GraphQL client + CLI against a local mock server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from urllib.parse import parse_qs, urlparse

from fakes import POOL_TYPES, build_chain, build_events, cex_klines, cp_ts

CHAIN = build_chain(n_cp=40_000)
EVENTS = build_events(n_cp=40_000)
LATEST = max(t["effects"]["checkpoint"]["sequenceNumber"] for t in CHAIN)
SEEN = {"max_first": 0, "errors_sent": 0}


def match(t, f):
    cp = t["effects"]["checkpoint"]["sequenceNumber"]
    if "afterCheckpoint" in f and not cp > f["afterCheckpoint"]:
        return False
    if "beforeCheckpoint" in f and not cp < f["beforeCheckpoint"]:
        return False
    if "sentAddress" in f and t["sender"]["address"] != f["sentAddress"]:
        return False
    return True


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # Binance klines mock
        q = parse_qs(urlparse(self.path).query)
        SEEN["binance"] = SEEN.get("binance", 0) + 1
        self._send(cex_klines(int(q["startTime"][0]), int(q["endTime"][0])))

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        q, v = req["query"], req["variables"]
        if "query Latest" in q:
            data = {"checkpoint": {"sequenceNumber": LATEST, "timestamp": cp_ts(LATEST)}}
        elif "multiGetCheckpoints" in q:
            data = {"multiGetCheckpoints": [{"sequenceNumber": k, "timestamp": cp_ts(k)} for k in v["keys"]]}
        elif "availableRange" in q:
            data = {"serviceConfig": {"availableRange": {"first": {"sequenceNumber": 0}, "last": {"sequenceNumber": LATEST}}}}
        elif "coinMetadata" in q:
            data = {"coinMetadata": {"decimals": 6, "symbol": "USDC" if "usdc" in v["t"] else "DEEP"}}
        elif "query EvPage" in q:
            f = v["filter"]
            sel = [e for e in EVENTS if e["contents"]["type"]["repr"] == f["type"]
                   and f["afterCheckpoint"] < e["transaction"]["effects"]["checkpoint"]["sequenceNumber"] < f["beforeCheckpoint"]]
            s = int(v["after"]) if v.get("after") else 0
            chunk = sel[s:s + v["first"]]
            data = {"events": {"pageInfo": {"hasNextPage": s + len(chunk) < len(sel), "endCursor": str(s + len(chunk))},
                               "nodes": chunk}}
        elif "multiGetObjects" in q:
            data = {"multiGetObjects": [
                {"address": k["address"], "asMoveObject": {"contents": {"type": {"repr": POOL_TYPES[k["address"]]}}}}
                if k["address"] in POOL_TYPES else None for k in v["keys"]]}
        elif "transactions(" in q:
            size = v.get("first") or v.get("last")
            if size > 10:  # pretend server limit -> client must shrink page
                SEEN["errors_sent"] += 1
                return self._send({"data": None, "errors": [{"message": "Query exceeds max output nodes"}]})
            SEEN["max_first"] = max(SEEN["max_first"], size)
            sel = [t for t in CHAIN if match(t, v["filter"])]
            if "first" in v and v["first"]:
                s = int(v["after"]) if v.get("after") else 0
                chunk = sel[s:s + size]
                data = {"transactions": {"pageInfo": {"hasNextPage": s + size < len(sel), "endCursor": str(s + len(chunk))},
                                         "nodes": chunk}}
            else:
                e = int(v["before"]) if v.get("before") else len(sel)
                s = max(0, e - size)
                data = {"transactions": {"pageInfo": {"hasPreviousPage": s > 0, "startCursor": str(s)},
                                         "nodes": sel[s:e]}}
        else:
            data = None
        self._send({"data": data})

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def test_cli_against_mock_server(tmp_path, capsys, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/graphql"
    monkeypatch.setenv("SUI_SCANNER_BINANCE_HOSTS", f"http://127.0.0.1:{srv.server_port}")
    from sui_scanner.__main__ import main
    common = ["--endpoint", url, "--db", str(tmp_path / "s.db"), "--rps", "0", "--page", "20"]
    assert main(["check"] + common) == 0
    assert main(["all"] + common + ["--days", "0.1", "--windows", "6", "--window-cps", "300",
                                     "--gap-windows", "3", "--gap-window-cps", "2400",
                                     "--sui-price", "3", "--out", str(tmp_path / "r.md"),
                                     "--csv", str(tmp_path / "b.csv")]) == 0
    srv.shutdown()
    out = (tmp_path / "r.md").read_text()
    assert "Decision gate" in out and "0xa111" in out
    assert "## 4. Gap lifetime" in out and "Median DEX lag" not in out  # flat CEX: no moves
    assert "| DEX prints compared |" in out and SEEN.get("binance", 0) >= 1
    assert "Operators (wallets grouped)" in out
    assert SEEN["errors_sent"] >= 1 and SEEN["max_first"] == 10  # page shrank 20 -> 10
    assert (tmp_path / "b.csv").read_text().count("\n") >= 3
