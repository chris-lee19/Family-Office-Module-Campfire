"""Replaying transactions into positions: cash, FIFO tax lots, cost basis, realized gains and income.

Everything here is derived, never stored, so it always agrees with the transactions. Conventions:
- Transaction amounts are gross and positive; the type gives the direction.
- Unitized instruments (listed securities) carry FIFO lots. Non-unitized holdings (fund interests,
  property) carry a single cost basis: contributions and purchases add, return of capital subtracts,
  and a sale is a full exit.
- Cash moves only in accounts that track cash (custody accounts).

Not yet handled: corporate actions (splits, mergers), specific-lot relief, wash sales, FX.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from investments.models import Account, Transaction

T = Transaction.Type
ZERO = Decimal(0)
LONG_TERM_DAYS = 365


class LedgerError(Exception):
    pass


@dataclass
class Lot:
    acquired_on: date
    quantity: Decimal
    cost: Decimal


@dataclass(frozen=True)
class Realized:
    account_id: int
    instrument_id: int
    sold_on: date
    acquired_on: date | None
    quantity: Decimal | None
    proceeds: Decimal
    cost: Decimal

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def term(self) -> str:
        if self.acquired_on is None:
            return "long"
        return "long" if (self.sold_on - self.acquired_on).days > LONG_TERM_DAYS else "short"


@dataclass
class AccountState:
    account: Account
    cash: Decimal = ZERO
    lots: dict[int, deque[Lot]] = field(default_factory=lambda: defaultdict(deque))
    cost: dict[int, Decimal] = field(default_factory=lambda: defaultdict(Decimal))  # non-unitized
    realized: list[Realized] = field(default_factory=list)
    income: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))  # by transaction type
    expenses: Decimal = ZERO

    def quantity(self, instrument_id: int) -> Decimal:
        return sum((lot.quantity for lot in self.lots.get(instrument_id, ())), ZERO)

    def cost_basis(self, instrument_id: int) -> Decimal:
        if instrument_id in self.lots:
            return sum((lot.cost for lot in self.lots[instrument_id]), ZERO)
        return self.cost.get(instrument_id, ZERO)

    def instrument_ids(self) -> set[int]:
        held = {i for i, lots in self.lots.items() if any(lot.quantity for lot in lots)}
        return held | {i for i, c in self.cost.items() if c}


CASH_IN = {T.DIVIDEND, T.INTEREST, T.DEPOSIT, T.DISTRIBUTION_ROC, T.DISTRIBUTION_INCOME, T.DISTRIBUTION_GAIN}
CASH_OUT = {T.BUY, T.FEE, T.WITHDRAWAL, T.CONTRIBUTION}


def _cash_effect(txn: Transaction) -> Decimal:
    if txn.txn_type == T.BUY:
        return -(txn.amount + txn.fees)
    if txn.txn_type == T.SELL:
        return txn.amount - txn.fees
    if txn.txn_type in CASH_IN:
        return txn.amount
    if txn.txn_type in CASH_OUT:
        return -txn.amount
    return ZERO  # transfers of securities


def _relieve_fifo(state: AccountState, txn: Transaction, *, realize: bool) -> None:
    lots = state.lots[txn.instrument_id]
    remaining = txn.quantity or ZERO
    held = state.quantity(txn.instrument_id)
    if remaining > held:
        raise LedgerError(f"{txn.trade_date}: {txn.get_txn_type_display()} of {remaining} {txn.instrument} but only {held} held in {state.account}")
    net_proceeds = txn.amount - txn.fees
    total_qty = remaining
    allocated = ZERO
    while remaining > 0:
        lot = lots[0]
        take = min(lot.quantity, remaining)
        # Whole lot / last slice take the exact remainder so rounding never leaves pennies behind.
        cost = lot.cost if take == lot.quantity else (lot.cost * take / lot.quantity).quantize(Decimal("0.01"))
        if realize:
            last = take == remaining
            proceeds = net_proceeds - allocated if last else (net_proceeds * take / total_qty).quantize(Decimal("0.01"))
            allocated += proceeds
            state.realized.append(
                Realized(state.account.pk, txn.instrument_id, txn.trade_date, lot.acquired_on, take, proceeds, cost)
            )
        lot.quantity -= take
        lot.cost -= cost
        remaining -= take
        if lot.quantity == 0:
            lots.popleft()


def _apply(state: AccountState, txn: Transaction) -> None:
    if state.account.tracks_cash:
        state.cash += _cash_effect(txn)
    t, instrument = txn.txn_type, txn.instrument
    if t in (T.DIVIDEND, T.INTEREST, T.DISTRIBUTION_INCOME, T.DISTRIBUTION_GAIN):
        state.income[t] += txn.amount
        return
    if t == T.FEE:
        state.expenses += txn.amount
        return
    if instrument is None:
        return  # deposits / withdrawals
    if instrument.is_unitized:
        if t == T.BUY:
            state.lots[instrument.pk].append(Lot(txn.trade_date, txn.quantity, txn.amount + txn.fees))  # type: ignore[arg-type]
        elif t == T.TRANSFER_IN:
            state.lots[instrument.pk].append(Lot(txn.acquired_on or txn.trade_date, txn.quantity, txn.amount))  # type: ignore[arg-type]
        elif t == T.SELL:
            _relieve_fifo(state, txn, realize=True)
        elif t == T.TRANSFER_OUT:
            _relieve_fifo(state, txn, realize=False)
        return
    # Non-unitized: one cost basis per holding.
    if t == T.CONTRIBUTION:
        state.cost[instrument.pk] += txn.amount
    elif t in (T.BUY, T.TRANSFER_IN):
        state.cost[instrument.pk] += txn.amount + txn.fees
    elif t == T.DISTRIBUTION_ROC:
        basis = state.cost[instrument.pk]
        returned = min(basis, txn.amount)
        state.cost[instrument.pk] = basis - returned
        if txn.amount > returned:  # return of capital beyond basis is a gain
            state.realized.append(Realized(state.account.pk, instrument.pk, txn.trade_date, None, None, txn.amount - returned, ZERO))
    elif t in (T.SELL, T.TRANSFER_OUT):
        basis = state.cost.pop(instrument.pk, ZERO)
        if t == T.SELL:
            state.realized.append(Realized(state.account.pk, instrument.pk, txn.trade_date, None, None, txn.amount - txn.fees, basis))


def replay(account: Account, as_of: date) -> AccountState:
    state = AccountState(account)
    for txn in account.transactions.filter(trade_date__lte=as_of).select_related("instrument").order_by("trade_date", "id"):
        _apply(state, txn)
    return state
