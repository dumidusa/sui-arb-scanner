"""Group bot wallets that belong to one operator.

Two simple, explainable links (each link is shown in the report):
  1. PRIVATE PACKAGE  — two bot wallets call the same Move package, and in the
     sample that package is only ever called by bot wallets (a bot's own contract).
  2. SWEEP            — bot wallets send coins to the same address (profit
     collection), or one bot wallet sends coins to another bot wallet.
     Addresses that ALSO receive from normal users (exchange deposit
     addresses, apps) are ignored, so unrelated bots are not merged.

System packages (0x1, 0x2, 0x3) and known DEX packages are never used as links.
A public package that only bots happened to call in the sample (e.g. a flash-loan
lender) could wrongly merge operators: every link is listed in the report so you
can check it, and --public-package PKG excludes a package.
"""

from __future__ import annotations

import json
from collections import defaultdict

from .classify import norm_addr
from .swaps import DEX_PACKAGES

SYSTEM = {norm_addr(x) for x in ("0x1", "0x2", "0x3", "0xdee9")}


class UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def group_bots(store, bots: set[str], public_packages=()) -> tuple[dict[str, str], list[tuple[str, str, str, str]]]:
    """Return (wallet -> operator id, links[(a, b, kind, via)])."""
    callers = defaultdict(set)   # package -> senders (sample: everyone)
    bot_pkgs = defaultdict(set)  # bot -> packages
    recipients = defaultdict(set)  # recipient -> bots that sent to it
    public_recipients = set()      # also paid by non-bots (exchanges, apps) -> not a link

    for sender, calls, recips, stage in store.query(
            "SELECT sender, calls, recipients, stage FROM txs WHERE calls IS NOT NULL"):
        pk = set(json.loads(calls or "[]"))
        if stage == "sample":
            for p in pk:
                callers[p].add(sender)
        rs = json.loads(recips or "[]")
        if sender in bots:
            bot_pkgs[sender] |= pk
            for r in rs:
                if r != sender:
                    recipients[r].add(sender)
        else:
            public_recipients.update(rs)

    private = {p for p, who in callers.items()
               if who and who <= bots and p not in SYSTEM and p not in DEX_PACKAGES}
    private -= {norm_addr(x) for x in public_packages}

    uf = UF()
    links = []
    for b in bots:
        uf.find(b)
    pkg_users = defaultdict(set)
    for b, pk in bot_pkgs.items():
        for p in pk & private:
            pkg_users[p].add(b)
    for p, users in pkg_users.items():
        users = sorted(users)
        for other in users[1:]:
            if uf.find(users[0]) != uf.find(other):
                links.append((users[0], other, "private package", p))
            uf.union(users[0], other)
    for r, senders in recipients.items():
        if r in public_recipients:
            continue
        senders = sorted(senders)
        if r in bots:
            for s in senders:
                if uf.find(s) != uf.find(r):
                    links.append((s, r, "sweep", r))
                uf.union(s, r)
        for other in senders[1:]:
            if uf.find(senders[0]) != uf.find(other):
                links.append((senders[0], other, "sweep", r))
            uf.union(senders[0], other)
    return {b: uf.find(b) for b in bots}, links
