"""성과 리포트: quantstats로 SPY와 비교한 HTML 리포트를 만든다.

두 가지를 만든다.
- paper: 실제 모의투자 기록(paper/equity.jsonl). 입금·출금은 수익으로 치지 않는다(performance.growth).
- backtest: 지금 전략(run_trader 기본값)을 2010년부터 백테스트한 결과. 야후에서 가격을 받는다.

quantstats 리포트는 영어라서, 맨 위에 주요 용어를 한국어로 풀어 쓴 안내 상자를 붙인다.
"""
from __future__ import annotations

import html
import json
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

from quant import performance

BACKTEST_START = "2009-01-01"
BACKTEST_COST_BPS = 10.0  # 실계좌 수수료 수준(편도 0.1%)
MIN_DAYS = 2              # 수익률이 이보다 적으면 리포트 대신 안내 페이지

_cache: dict[str, str] = {}  # 하루에 한 번만 백테스트 가격을 받는다

GLOSSARY = [
    ("Cumulative Return", "누적 수익률 (시작부터 지금까지)"),
    ("CAGR", "연평균 수익률 (1년으로 환산)"),
    ("Sharpe", "샤프 지수: 흔들림 대비 수익. 높을수록 좋고, 1 이상이면 우수"),
    ("Sortino", "소르티노: 샤프와 같지만 하락 흔들림만 따짐"),
    ("Max Drawdown", "최대 낙폭: 고점에서 가장 많이 떨어졌던 비율"),
    ("Longest DD Days", "고점을 회복하는 데 가장 오래 걸린 날 수"),
    ("Volatility (ann.)", "연간 변동성: 수익률이 얼마나 출렁이는지"),
    ("Win Days %", "오른 날의 비율"),
    ("Worst 5 Drawdowns", "가장 깊었던 낙폭 다섯 번"),
    ("Monthly Returns", "월별 수익률 표 (빨강 손실, 초록 이익)"),
]


def paper_returns(paper: Path) -> tuple[pd.Series, pd.Series | None]:
    """Daily returns of the paper account (flows excluded) and of the benchmark."""
    rows = _read_jsonl(paper / "equity.jsonl")
    state = _read_json(paper / "state.json")
    if not rows:
        return pd.Series(dtype=float), None
    idx = pd.to_datetime([r["date"] for r in rows])
    g = pd.Series(performance.growth(rows, state.get("initial_capital")), index=idx)
    strat = g.div(g.shift(1).fillna(1.0)) - 1
    if not state.get("initial_capital"):
        strat = strat.iloc[1:]
    closes = [r.get("benchmark_close") for r in rows]
    bench = None
    if all(c for c in closes):
        bench = pd.Series([float(c) for c in closes], index=idx).pct_change().fillna(0.0)
        bench = bench.reindex(strat.index)
    return strat, bench


def backtest_returns(close: pd.DataFrame | None = None, cost_bps: float = BACKTEST_COST_BPS
                     ) -> tuple[pd.Series, pd.Series, str]:
    """Daily net returns of the current strategy and of SPY, plus the strategy name."""
    from quant.data import load_prices
    from quant.trader.runner import BENCHMARK, DATA_TICKERS, TraderConfig, model_weights
    from scripts.run_trader import STRATEGIES

    cfg = TraderConfig(**STRATEGIES["canary_eem"])
    if close is None:
        tickers = sorted(set(DATA_TICKERS) | set(cfg.canary))
        close = load_prices(tickers, BACKTEST_START, use_cache=False).dropna()
    w = model_weights(close, cfg)
    ret = close[w.columns].pct_change().fillna(0.0)
    net = (w.shift(1).fillna(0.0) * ret).sum(axis=1) - w.diff().abs().sum(axis=1) * cost_bps / 10_000
    # 첫 1년은 모멘텀(252일)을 계산할 수 없어 현금이므로 뺀다.
    invested = w.sum(axis=1) > 0
    start = invested.idxmax() if invested.any() else net.index[0]
    net = net[net.index > start]
    bench = close[BENCHMARK].pct_change().reindex(net.index).fillna(0.0)
    return net, bench, "EEM 카나리아 + 4분할"


def build(kind: str, paper: Path) -> str:
    """HTML of the requested report ('paper' or 'backtest'). Never raises."""
    try:
        if kind == "backtest":
            key = f"backtest-{date.today().isoformat()}"
            if key not in _cache:
                strat, bench, name = backtest_returns()
                _cache.clear()
                _cache[key] = tearsheet(strat, bench, f"{name} · 장기 백테스트",
                                        f"지금 전략을 {strat.index[0]:%Y-%m-%d}부터 백테스트한 결과예요 "
                                        f"(수수료 편도 {BACKTEST_COST_BPS / 100:.1f}% 반영). "
                                        "과거 결과이고 앞으로도 같다는 보장은 없어요.")
            return _cache[key]
        strat, bench = paper_returns(paper)
        if len(strat) < MIN_DAYS:
            return notice("모의투자 기록이 아직 부족해요",
                          f"리포트를 만들려면 거래일 기록이 {MIN_DAYS}일 이상 필요해요. "
                          "며칠 뒤에 다시 열어 보세요. 그동안은 '장기 백테스트' 리포트를 볼 수 있어요.")
        note = (f"모의투자 {len(strat)}거래일 기록이에요. "
                "기간이 짧으면 연평균 수익률과 샤프 같은 숫자가 크게 출렁이니 참고만 하세요."
                if len(strat) < 60 else f"모의투자 {len(strat)}거래일 기록이에요.")
        return tearsheet(strat, bench, "모의투자 기록", note)
    except Exception as e:  # noqa: BLE001 - the page shows the reason instead of a crash
        return notice("리포트를 만들지 못했어요", f"{type(e).__name__}: {e}")


def tearsheet(strat: pd.Series, bench: pd.Series | None, title: str, note: str) -> str:
    import matplotlib
    matplotlib.use("Agg")  # the agent has no screen; draw into files only
    import quantstats as qs

    strat = strat.rename("Strategy")
    if bench is not None:
        bench = bench.rename("SPY")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.html"
        qs.reports.html(strat, benchmark=bench, output=str(out), title=title,
                        download_filename="mintory-report.html")
        page = out.read_text(encoding="utf-8")
    return _add_guide(page, title, note)


def _add_guide(page: str, title: str, note: str) -> str:
    rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>" for k, v in GLOSSARY)
    box = (
        '<div style="max-width:960px;margin:16px auto;padding:14px 18px;border:1px solid #ddd;'
        'border-radius:10px;background:#fafafa;font:14px/1.6 \'Malgun Gothic\',sans-serif;color:#222">'
        f"<b>Mintory 성과 리포트 · {html.escape(title)}</b><br>{html.escape(note)}<br>"
        "Strategy = 우리 전략, SPY = 미국 S&amp;P 500 지수 ETF(비교 기준)."
        "<details style=\"margin-top:6px\"><summary>영어 용어 풀이</summary>"
        f'<table style="margin-top:6px;border-collapse:collapse">{rows}</table></details></div>'
    )
    i = page.find("<body")
    if i < 0:
        return box + page
    j = page.find(">", i) + 1
    return page[:j] + box + page[j:]


def notice(title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        '<body style="background:#0f1115;color:#e8e8ea;font:15px/1.7 \'Malgun Gothic\',sans-serif;'
        'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
        f'<div style="max-width:520px;padding:24px"><h2 style="margin:0 0 8px">{html.escape(title)}</h2>'
        f"<p>{html.escape(message)}</p></div></body></html>"
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
