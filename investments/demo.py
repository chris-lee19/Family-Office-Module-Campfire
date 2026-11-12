"""Investments for the fictional Alder family. Dates are relative to `today` so the demo never goes stale.

What the data is built to show:
- Liquid custody accounts with FIFO lots and a realized long-term gain.
- An IRA owned by a person (tax-deferred), next to taxable and tax-exempt (foundation) accounts.
- Two private funds owned by the family LP: called / unfunded / distributions / NAV / TVPI / IRR,
  a recallable distribution, and a capital call that is due and not yet paid.
- A NAV statement that jumps ~480% (a mis-read document) and is held for review, not booked.
- A directly held property whose appraisal is older than the stale threshold.
"""

from datetime import date, timedelta
from decimal import Decimal

from entities.models import Entity, Family, Person
from investments.models import Account, CapitalCall, Commitment, ImportBatch, Instrument, Price, Transaction, Valuation
from investments.services.valuation import record_valuation
from investments.taxonomy import ensure_default_taxonomy

T = Transaction.Type
K = Instrument.Kind
A = Account.AccountType
TT = Account.TaxTreatment
CUSTODIAN = "Bayview Securities (fictional custodian)"


def D(value: str) -> Decimal:
    return Decimal(value)


def seed_alder_investments(family: Family, today: date) -> None:
    ws = family.workspace
    classes = ensure_default_taxonomy(ws)

    def day(offset: int) -> date:
        return today + timedelta(days=offset)

    def instrument(name: str, kind: str, ac: str, ticker: str = "", **kw) -> Instrument:  # type: ignore[no-untyped-def]
        return Instrument.objects.create(
            workspace=ws, name=name, kind=kind, asset_class=classes[ac], ticker=ticker,
            is_unitized=kind in Instrument.UNITIZED_KINDS, **kw,
        )

    ustm = instrument("US Total Market Index ETF", K.FUND, "public_equity", "USTM")
    intd = instrument("International Developed Equity ETF", K.FUND, "public_equity", "INTD")
    bond = instrument("US Treasury 4.25% 2030", K.FIXED_INCOME, "fixed_income", "UST30")
    mmf = instrument("Government Money Market Fund", K.CASH_EQUIVALENT, "cash", "GOVMM")
    summit = instrument("Summit Growth Partners IV, L.P.", K.PRIVATE_FUND, "private_equity", sponsor="Summit Growth Partners", vintage_year=today.year - 4)
    crescent = instrument("Crescent Venture Fund II, L.P.", K.PRIVATE_FUND, "venture_capital", sponsor="Crescent Ventures", vintage_year=today.year - 3)
    harbor = instrument("Harbor Street Mixed-Use Property", K.REAL_ESTATE, "real_estate")

    for inst, px in ((ustm, "272.40"), (intd, "61.15"), (bond, "992.10"), (mmf, "1.00")):
        Price.objects.create(instrument=inst, as_of=day(-2), price=D(px), source="demo")

    entity = {e.name: e for e in Entity.objects.filter(family=family)}
    person = {p.first_name: p for p in Person.objects.filter(family=family)}
    batch = ImportBatch.objects.create(family=family, source="demo", reference="Alder demo seed")

    def account(name: str, owner: Entity | Person, kind: str, tax: str = TT.TAXABLE, tracks_cash: bool = True, **kw) -> Account:  # type: ignore[no-untyped-def]
        return Account.objects.create(
            family=family, name=name, account_type=kind, tax_treatment=tax, tracks_cash=tracks_cash,
            entity=owner if isinstance(owner, Entity) else None, person=owner if isinstance(owner, Person) else None, **kw,
        )

    seq = iter(range(1, 10_000))

    def txn(acct: Account, t: str, when: int, amount: str, inst: Instrument | None = None, qty: str | None = None, **kw) -> Transaction:  # type: ignore[no-untyped-def]
        return Transaction.objects.create(
            account=acct, instrument=inst, txn_type=t, trade_date=day(when), amount=D(amount),
            quantity=D(qty) if qty else None, batch=batch, source_ref=f"demo-{next(seq)}", **kw,
        )

    # Robert's revocable trust: taxable custody account.
    rrt = account("Robert Alder RT – Custody", entity["Robert Alder Revocable Trust"], A.BROKERAGE, custodian=CUSTODIAN, account_number_last4="4417")
    txn(rrt, T.DEPOSIT, -900, "2000000")
    txn(rrt, T.BUY, -899, "1260000", ustm, "6000")
    txn(rrt, T.BUY, -899, "440000", intd, "8000")
    txn(rrt, T.DIVIDEND, -700, "9500", ustm)
    txn(rrt, T.BUY, -400, "240000", ustm, "1000")
    txn(rrt, T.DIVIDEND, -300, "12000", ustm)
    txn(rrt, T.SELL, -200, "530000", ustm, "2000", fees=D("25"))

    # Eleanor's IRA: owned by a person, tax-deferred.
    ira = account("Eleanor Alder – IRA", person["Eleanor"], A.RETIREMENT, TT.TAX_DEFERRED, custodian=CUSTODIAN, account_number_last4="8802")
    txn(ira, T.TRANSFER_IN, -1200, "180000", ustm, "1500", acquired_on=day(-2400))
    txn(ira, T.DEPOSIT, -1200, "25000")

    # Dynasty trust: bonds and equity.
    dyn = account("Alder 2012 Dynasty Trust – Custody", entity["Alder 2012 Dynasty Trust"], A.BROKERAGE, custodian=CUSTODIAN, account_number_last4="1290")
    txn(dyn, T.DEPOSIT, -1000, "3000000")
    txn(dyn, T.BUY, -990, "1470000", bond, "1500")
    txn(dyn, T.BUY, -990, "920000", ustm, "4000")
    txn(dyn, T.INTEREST, -800, "31875", bond)
    txn(dyn, T.INTEREST, -620, "31875", bond)
    txn(dyn, T.INTEREST, -440, "31875", bond)
    txn(dyn, T.INTEREST, -260, "31875", bond)
    txn(dyn, T.INTEREST, -80, "31875", bond)

    # Foundation: tax-exempt.
    fdn = account("Alder Family Foundation – Custody", entity["Alder Family Foundation"], A.BROKERAGE, TT.TAX_EXEMPT, custodian=CUSTODIAN, account_number_last4="5530")
    txn(fdn, T.DEPOSIT, -700, "1500000")
    txn(fdn, T.BUY, -690, "560000", intd, "10000")
    txn(fdn, T.BUY, -690, "900000", mmf, "900000")

    lp = entity["Alder Family Investments LP"]

    # Summit IV: mature PE fund, 80% called, distributions incl. a recallable one, call pending.
    s_acct = account("AFI LP – Summit Growth Partners IV", lp, A.PRIVATE_INVESTMENT, tracks_cash=False, source="document_ai")
    s_commit = Commitment.objects.create(account=s_acct, instrument=summit, amount=D("5000000"), commitment_date=day(-1300))
    for when, amt in ((-1250, "1000000"), (-1000, "1250000"), (-700, "1000000"), (-400, "750000")):
        txn(s_acct, T.CONTRIBUTION, when, amt, summit)
    record_valuation(account=s_acct, instrument=summit, as_of=day(-500), value=D("3500000"), method=Valuation.Method.NAV_STATEMENT, batch=batch)
    txn(s_acct, T.DISTRIBUTION_ROC, -300, "200000", summit, is_recallable=True)
    record_valuation(account=s_acct, instrument=summit, as_of=day(-230), value=D("4450000"), method=Valuation.Method.NAV_STATEMENT, batch=batch)
    txn(s_acct, T.DISTRIBUTION_GAIN, -150, "300000", summit)
    record_valuation(account=s_acct, instrument=summit, as_of=day(-45), value=D("4700000"), method=Valuation.Method.NAV_STATEMENT, batch=batch)
    CapitalCall.objects.create(commitment=s_commit, notice_date=day(-5), due_date=day(10), amount=D("500000"), source_ref="demo-notice-summit-iv")

    # Crescent II: VC fund; latest NAV statement mis-read (685,000 read as 6,850,000) and held.
    c_acct = account("AFI LP – Crescent Venture Fund II", lp, A.PRIVATE_INVESTMENT, tracks_cash=False, source="document_ai")
    Commitment.objects.create(account=c_acct, instrument=crescent, amount=D("2000000"), commitment_date=day(-1000))
    for when, amt in ((-950, "400000"), (-600, "400000"), (-250, "300000")):
        txn(c_acct, T.CONTRIBUTION, when, amt, crescent)
    record_valuation(account=c_acct, instrument=crescent, as_of=day(-320), value=D("950000"), method=Valuation.Method.NAV_STATEMENT, batch=batch)
    record_valuation(account=c_acct, instrument=crescent, as_of=day(-140), value=D("1180000"), method=Valuation.Method.NAV_STATEMENT, batch=batch)
    record_valuation(account=c_acct, instrument=crescent, as_of=day(-40), value=D("6850000"), method=Valuation.Method.NAV_STATEMENT, batch=batch, source_ref="demo-crescent-q-statement.pdf")

    # Direct real estate: appraisal older than the stale threshold.
    re_acct = account("ARE LLC – Harbor Street Property", entity["Alder Real Estate Holdings LLC"], A.REAL_ASSET, tracks_cash=False)
    txn(re_acct, T.BUY, -1500, "8500000", harbor, fees=D("120000"))
    record_valuation(account=re_acct, instrument=harbor, as_of=day(-500), value=D("10200000"), method=Valuation.Method.APPRAISAL, batch=batch)
