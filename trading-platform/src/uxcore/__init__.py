"""uxcore — UnifiedExchange Core.

A hard fork of CCXT whose *additions* live here, so that ``git merge upstream/master``
stays a five-minute job. Nothing in this package changes the semantics of a CCXT unified
method; it adds reliability, rate-limit safety, feed correctness and a plugin seam.
"""
from .errors import ErrorClass, RetryPolicy, UXError, classify
from .exchange import UXExchangeMixin, make_exchange
from .plugin_registry import Plugin, register
from .ratelimit import AdaptiveRateLimiter
from .resilience import CircuitBreaker, resilient
from .ws import FeedHealth, FeedMonitor

__all__ = [
    'AdaptiveRateLimiter', 'CircuitBreaker', 'ErrorClass', 'FeedHealth', 'FeedMonitor',
    'Plugin', 'RetryPolicy', 'UXError', 'UXExchangeMixin', 'classify', 'make_exchange',
    'register', 'resilient',
]
__version__ = '0.1.0'
