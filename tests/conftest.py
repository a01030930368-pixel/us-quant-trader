import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def prices():
    """Synthetic random-walk prices for 5 assets over ~12 years."""
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2010-01-01", periods=252 * 12)
    drift = np.array([0.0004, 0.0005, 0.0002, 0.0001, 0.0003])
    rets = rng.normal(drift, 0.01, size=(len(idx), 5))
    return pd.DataFrame(100 * np.cumprod(1 + rets, axis=0), index=idx,
                        columns=["SPY", "QQQ", "IEF", "GLD", "SHY"])
