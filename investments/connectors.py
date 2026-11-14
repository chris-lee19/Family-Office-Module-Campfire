"""Connectors: where the family office's data comes from, and what can be connected.

The catalog is honest about what exists. "Connected" and "By file" work today (Campfire itself, its bank feed, the
custodian file importer, the onboarding workbook). "On request" names the tools family offices commonly use that have
no adapter yet: asking for one records the request, so adapters are built in the order offices need them.
"""

from dataclasses import dataclass

CONNECTED, BY_FILE, ON_REQUEST = "connected", "file", "request"


@dataclass(frozen=True)
class Connector:
    key: str
    name: str
    group: str
    status: str
    brings: str  # what data it brings in
    how: str  # how it works today, or would
    target: str = ""  # page to open, for the ones that work today


GROUPS = ("Books and banking", "Custodians and brokers", "Portfolio reporting and aggregation", "Private investments and documents")
CATALOG = [
    Connector("campfire", "Campfire", GROUPS[0], CONNECTED, "General ledger, entities, bills, payees, financial statements",
              "Signed in and connected. The books live in Campfire; this app reads and writes them.", "gl:statements"),
    Connector("bank-feeds", "Bank accounts (through Campfire)", GROUPS[0], CONNECTED, "Balances and transactions of operating bank accounts",
              "Banks are linked in Campfire's cash management; their lines arrive on the Cash page to categorize.", "gl:cash"),
    Connector("custodian-files", "Custodian activity and price files", GROUPS[1], BY_FILE, "Trades, income, fees, transfers and prices from any custodian",
              "Upload the custodian's export. Layouts can be set per custodian; every row is checked before anything is imported.", "investments:imports"),
    Connector("onboarding", "Onboarding workbook", GROUPS[1], BY_FILE, "A whole family: entities, ownership, accounts, opening positions",
              "One Excel workbook loads the structure and opening balances; the same workbook exports them again.", "records:onboarding"),
    Connector("jpmorgan", "J.P. Morgan Private Bank", GROUPS[1], ON_REQUEST, "Positions, transactions and cash for custody and bank accounts",
              "Today: its activity export through the file importer. A direct feed needs the bank's data-sharing agreement for the family."),
    Connector("schwab", "Charles Schwab", GROUPS[1], ON_REQUEST, "Positions, transactions and cost basis", "Today: its activity export through the file importer."),
    Connector("fidelity", "Fidelity", GROUPS[1], ON_REQUEST, "Positions, transactions and cost basis", "Today: its activity export through the file importer."),
    Connector("pershing", "BNY Pershing", GROUPS[1], ON_REQUEST, "Positions and transactions", "Today: a Pershing-style layout is included in the file importer."),
    Connector("northern-trust", "Northern Trust", GROUPS[1], ON_REQUEST, "Custody positions, transactions and trust accounting",
              "Today: its activity export through the file importer."),
    Connector("goldman", "Goldman Sachs Private Wealth", GROUPS[1], ON_REQUEST, "Positions and transactions", "Today: its activity export through the file importer."),
    Connector("morgan-stanley", "Morgan Stanley", GROUPS[1], ON_REQUEST, "Positions and transactions", "Today: its activity export through the file importer."),
    Connector("addepar", "Addepar", GROUPS[2], ON_REQUEST, "Aggregated holdings, transactions and valuations across custodians",
              "Would read holdings and transactions from Addepar's API into the investment records, so they are keyed once."),
    Connector("masttro", "Masttro", GROUPS[2], ON_REQUEST, "Aggregated net worth, holdings and private-investment data",
              "Would read holdings and valuations from Masttro's API into the investment records."),
    Connector("black-diamond", "Black Diamond", GROUPS[2], ON_REQUEST, "Portfolio accounting positions and performance", "Would read positions and transactions."),
    Connector("orion", "Orion", GROUPS[2], ON_REQUEST, "Portfolio accounting positions and performance", "Would read positions and transactions."),
    Connector("arch", "Arch", GROUPS[3], ON_REQUEST, "Capital calls, distributions, statements and K-1s from fund portals",
              "Would bring call and distribution notices, NAV statements and K-1s into Capital calls, Review and the K-1 tracker."),
    Connector("canoe", "Canoe Intelligence", GROUPS[3], ON_REQUEST, "Data read from alternative-investment documents",
              "Would bring the figures from fund statements and notices into the investment records."),
    Connector("icapital", "iCapital", GROUPS[3], ON_REQUEST, "Fund subscriptions, capital activity and statements", "Would bring capital activity and statements."),
    Connector("carta", "Carta", GROUPS[3], ON_REQUEST, "Direct private-company holdings and valuations", "Would bring share positions and 409A or round valuations."),
]
BY_KEY = {c.key: c for c in CATALOG}
