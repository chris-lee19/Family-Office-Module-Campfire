"""The demo books for the Alder entities: every entry balances and the operating accounts never run dry."""

from decimal import Decimal

from gl.management.commands.campfire_demo_books import ARE, CASH, EQUITY, MORTGAGE, OPENING, month_lines, opening_lines


def test_opening_entries_balance_and_carry_the_investment_book() -> None:
    for entity in EQUITY:
        lines = opening_lines(entity, Decimal("69500"), Decimal("1940000"))
        assert sum(a for _, a, _ in lines) == 0
        assert lines[-1][0] == EQUITY[entity]  # equity is the balancing line
        assert {name for name, _, _ in lines} >= {"Cash held at custodians", "Investments at cost"}
    assert all(name != "Cash held at custodians" for name, _, _ in opening_lines(ARE, Decimal(0), Decimal("8620000")))


def test_each_month_balances_and_cash_stays_positive() -> None:
    mortgage = Decimal(-dict(OPENING[ARE])[MORTGAGE])
    cash = {entity: Decimal(dict(OPENING[entity])[CASH]) for entity in EQUITY}
    for month in range(1, 13):
        for entity in EQUITY:
            lines = month_lines(entity, month, mortgage)
            assert sum(a for _, a, _ in lines) == 0, (entity, month)
            cash[entity] += sum(a for name, a, _ in lines if name == CASH)
            assert cash[entity] > 0, (entity, month)
            if entity == ARE:
                principal = next(a for name, a, _ in lines if name == MORTGAGE)
                assert 0 < principal < 23_400
                mortgage -= principal
    assert Decimal("4100000") < mortgage < Decimal("4200000")
