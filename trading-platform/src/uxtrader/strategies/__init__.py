"""Strategy implementations. One module per strategy, specified in docs/01-strategies.md.

Read ``donchian_regime.py`` first — it is the fully worked reference and every other
strategy follows its shape.
"""
from .adaptive_grid import AdaptiveGrid
from .donchian_regime import DonchianRegime
from .funding_carry import FundingCarry
from .mr_vwap import VwapMeanReversion
from .pairs_statarb import PairsStatArb
from .squeeze_fade import SqueezeFade
from .xs_momentum import CrossSectionalMomentum

REGISTRY = {
    'S1': FundingCarry,
    'S2': CrossSectionalMomentum,
    'S3': PairsStatArb,
    'S4': DonchianRegime,
    'S5': VwapMeanReversion,
    'S6': AdaptiveGrid,
    'S7': SqueezeFade,
}

__all__ = ['REGISTRY', 'AdaptiveGrid', 'CrossSectionalMomentum', 'DonchianRegime',
           'FundingCarry', 'PairsStatArb', 'SqueezeFade', 'VwapMeanReversion']
