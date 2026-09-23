"""Order management, routing, execution algos, and the paper and live brokers.

Exports are lazy (PEP 562) to keep this package free of import-order cycles with
``uxtrader.lab``.
"""
from importlib import import_module

_EXPORTS = {
    'Broker': '.oms', 'OrderManager': '.oms', 'PaperBroker': '.paper',
    'LiveBroker': '.live', 'SmartRouter': '.router',
}
__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        return getattr(import_module(_EXPORTS[name], __name__), name)
    raise AttributeError(name)
