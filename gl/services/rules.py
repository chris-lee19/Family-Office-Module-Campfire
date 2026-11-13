"""Posting rules: each investment transaction as balanced debits and credits by role, at cost.

    type                 debit                                credit
    buy                  investments (amount + fees)          cash
    sell                 cash (amount - fees)                 investments (cost relieved), realized gain (rest)
    transfer in          investments (cost basis)             transfers clearing
    transfer out         transfers clearing (cost relieved)   investments
    dividend / interest  cash                                 dividend / interest income
    fee                  investment fees                      cash
    deposit              cash                                 transfers clearing
    withdrawal           transfers clearing                   cash
    contribution         investments                          cash
    distribution: ROC    cash                                 investments (cost relieved), realized gain (excess)
    distribution: income cash                                 fund income
    distribution: gain   cash                                 realized gain

"Cash" is the custody account's cash for accounts that hold cash, and investment cash clearing for holdings
whose money moves through a bank account booked in Campfire (fund interests): the bank feed categorizes the
wire against the same clearing account, which then nets to zero. A loss is a debit to realized gain / loss.

Cost relieved comes from the ledger replay (FIFO lots), so the GL agrees with the investment book.
Valuation (unrealized gain) entries are a separate, period-end step and are not posted here yet.
"""

from dataclasses import dataclass
from decimal import Decimal

from gl.models import Role
from investments.models import Transaction
from investments.services.ledger import AccountState

T = Transaction.Type
ZERO = Decimal(0)


class RuleError(Exception):
    pass


@dataclass(frozen=True)
class Leg:
    role: str
    amount: Decimal  # debit positive, credit negative
    txn: Transaction


def legs_for(txn: Transaction, state: AccountState) -> list[Leg]:
    cash = Role.CASH if txn.account.tracks_cash else Role.CASH_CLEARING
    a, fees, t = txn.amount, txn.fees, txn.txn_type
    relieved = state.relieved.get(txn.pk, ZERO)

    def pairs(*items: tuple[str, Decimal]) -> list[Leg]:
        return [Leg(role, amount, txn) for role, amount in items if amount]

    if t == T.BUY:
        legs = pairs((Role.INVESTMENTS, a + fees), (cash, -(a + fees)))
    elif t == T.SELL:
        net = a - fees
        legs = pairs((cash, net), (Role.INVESTMENTS, -relieved), (Role.REALIZED_GAIN, -(net - relieved)))
    elif t == T.TRANSFER_IN:
        legs = pairs((Role.INVESTMENTS, a), (Role.TRANSFERS, -a))
    elif t == T.TRANSFER_OUT:
        legs = pairs((Role.TRANSFERS, relieved), (Role.INVESTMENTS, -relieved))
    elif t == T.DIVIDEND:
        legs = pairs((cash, a), (Role.DIVIDEND_INCOME, -a))
    elif t == T.INTEREST:
        legs = pairs((cash, a), (Role.INTEREST_INCOME, -a))
    elif t == T.FEE:
        legs = pairs((Role.INVESTMENT_FEES, a), (cash, -a))
    elif t == T.DEPOSIT:
        legs = pairs((cash, a), (Role.TRANSFERS, -a))
    elif t == T.WITHDRAWAL:
        legs = pairs((Role.TRANSFERS, a), (cash, -a))
    elif t == T.CONTRIBUTION:
        legs = pairs((Role.INVESTMENTS, a), (cash, -a))
    elif t == T.DISTRIBUTION_ROC:
        legs = pairs((cash, a), (Role.INVESTMENTS, -relieved), (Role.REALIZED_GAIN, -(a - relieved)))
    elif t == T.DISTRIBUTION_INCOME:
        legs = pairs((cash, a), (Role.FUND_INCOME, -a))
    elif t == T.DISTRIBUTION_GAIN:
        legs = pairs((cash, a), (Role.REALIZED_GAIN, -a))
    else:
        raise RuleError(f"No posting rule for {t}.")
    if sum((leg.amount for leg in legs), ZERO) != ZERO:
        raise RuleError(f"Posting rule for {txn} doesn't balance.")
    return legs
