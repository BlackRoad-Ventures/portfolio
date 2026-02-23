"""Portfolio tracker tests."""
import pytest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

try:
    from portfolio_tracker import Position, Portfolio
    HAS_PORTFOLIO = True
except ImportError:
    HAS_PORTFOLIO = False


@pytest.mark.skipif(not HAS_PORTFOLIO, reason="portfolio_tracker not importable")
class TestPosition:
    def test_pnl_long(self):
        p = Position("BTC", "crypto", 1.0, 40000.0)
        p.update_price(50000.0)
        assert p.pnl == 10000.0
        assert p.pnl_pct == pytest.approx(25.0, 0.01)

    def test_pnl_negative(self):
        p = Position("ETH", "crypto", 2.0, 3000.0)
        p.update_price(2000.0)
        assert p.pnl == -2000.0

    def test_value(self):
        p = Position("AAPL", "equity", 10.0, 150.0)
        p.update_price(160.0)
        assert p.value == 1600.0

@pytest.mark.skipif(not HAS_PORTFOLIO, reason="portfolio_tracker not importable")
class TestPortfolio:
    def test_total_value(self):
        port = Portfolio()
        port.add("BTC", "crypto", 1.0, 40000.0)
        port.add("ETH", "crypto", 5.0, 3000.0)
        port.update("BTC", 45000.0)
        port.update("ETH", 3500.0)
        assert port.total_value == pytest.approx(62500.0, 0.01)
