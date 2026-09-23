"""Research lab: backtesting, metrics, walk-forward, optimization.

Exports are lazy (PEP 562): ``execution.paper`` imports ``lab.fills`` and
``lab.backtest`` imports ``execution.paper``, so eager re-exports here would make the
import order of the whole package matter.
"""
from importlib import import_module

_EXPORTS = {
    'BacktestResult': '.backtest', 'EventDrivenBacktester': '.backtest',
    'CostModel': '.fills', 'FillSimulator': '.fills', 'QueueModel': '.fills',
    'deflated_sharpe': '.metrics', 'pbo': '.metrics', 'summarize': '.metrics',
    'WalkForward': '.walkforward', 'WindowResult': '.walkforward',
    'VectorBacktester': '.vector', 'VectorResult': '.vector',
}
__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        return getattr(import_module(_EXPORTS[name], __name__), name)
    raise AttributeError(name)
