"""The platform as services. Each class runs identically on ``InMemoryBus`` (one
process: tests, single-box paper trading) and on ``NatsBus`` (one container each)."""
from .execution import ExecutionService
from .portfolio import PortfolioService
from .risk import RiskService
from .strategy_engine import StrategyEngine, StrategySpec

__all__ = ['ExecutionService', 'PortfolioService', 'RiskService', 'StrategyEngine',
           'StrategySpec']
