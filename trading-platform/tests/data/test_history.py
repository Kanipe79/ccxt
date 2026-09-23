"""Historical loader through the real ccxt binanceusdm parser, HTTP stubbed."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from uxtrader.data.history import (
    TF_MS, HistoryLoader, bars_from_frame, detect_gaps, funding_from_frame,
)
from uxtrader.data.store import ParquetStore

KLINES = ('GET', 'fapi/v1/klines')
FUNDING = ('GET', 'fapi/v1/fundingRate')
SYM = 'BTC/USDT:USDT'
H4 = TF_MS['4h']
T0 = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def kline(ts: int, close: float = 100.0) -> list:
    return [ts, str(close - 1), str(close + 2), str(close - 2), str(close), '10',
            ts + H4 - 1, '1000', 5, '5', '500', '0']


async def test_paginates_and_drops_forming_bar(ex, transport):
    page1 = [kline(T0 + i * H4) for i in range(3)]
    page2 = [kline(T0 + i * H4) for i in range(3, 5)]
    transport.on(*KLINES, page1, page2, [])
    # "now" is mid-way through bar #4, so bar #4 is still forming and must be dropped.
    loader = HistoryLoader(ex, now_ms=T0 + 4 * H4 + H4 // 2)
    df = await loader.ohlcv(SYM, '4h', T0, page_limit=3)

    assert len(df) == 4, 'forming bar leaked into the dataset'
    assert transport.count(*KLINES) == 2
    assert (df['available_ts'] > df['ts'] + pd.Timedelta(hours=4)).all()
    assert df['close'].dtype.kind == 'f'


async def test_since_is_honoured_and_duplicates_removed(ex, transport):
    overlap = [kline(T0 + i * H4) for i in range(2)]
    transport.on(*KLINES, overlap, overlap[1:] + [kline(T0 + 2 * H4)], [])
    loader = HistoryLoader(ex, now_ms=T0 + 10 * H4)
    df = await loader.ohlcv(SYM, '4h', T0, page_limit=2)
    assert list(df['ts'].diff().dropna().unique()) == [pd.Timedelta(hours=4)]
    assert len(df) == 3


def test_gap_detection():
    ts = [T0, T0 + H4, T0 + 4 * H4, T0 + 5 * H4]
    gaps = detect_gaps(ts, H4)
    assert len(gaps) == 1 and gaps[0].missing_bars == 2


async def test_funding_history_and_interval_inference(ex, transport):
    eight_h = 8 * 3_600_000
    rows = [{'symbol': 'BTCUSDT', 'fundingTime': T0 + i * eight_h,
             'fundingRate': '0.0001', 'markPrice': '42000'} for i in range(3)]
    transport.on(*FUNDING, rows, [])
    loader = HistoryLoader(ex, now_ms=T0 + 10 * eight_h)
    df = await loader.funding(SYM, T0)
    assert len(df) == 3
    assert (df['interval_hours'] == 8.0).all()
    events = list(funding_from_frame(df, 'binanceusdm', SYM))
    assert events[0].apr == pytest.approx(0.0001 * 3 * 365)


async def test_store_is_immutable_and_point_in_time(ex, transport, tmp_path):
    transport.on(*KLINES, [kline(T0 + i * H4, 100 + i) for i in range(6)], [])
    df = await HistoryLoader(ex, now_ms=T0 + 6 * H4).ohlcv(SYM, '4h', T0)
    store = ParquetStore(tmp_path)
    store.write('ohlcv', 'binanceusdm', SYM, df, build='b1', extra='4h')
    with pytest.raises(FileExistsError):
        store.write('ohlcv', 'binanceusdm', SYM, df, build='b1', extra='4h')

    as_of = datetime.fromtimestamp((T0 + 3 * H4) / 1000 + 5, tz=timezone.utc)
    pit = store.read('ohlcv', 'binanceusdm', SYM, extra='4h', as_of=as_of)
    # Bars 0,1,2 have closed by then; bar 2 closes exactly at T0+3*H4 (+2 s delay).
    assert len(pit) == 3

    bars = list(bars_from_frame(pit, 'binanceusdm', SYM, '4h'))
    assert bars[-1].close_ts <= as_of
