"""수익률 계산: 입금·출금을 뺀 '시간가중 수익률' (펀드 수익률과 같은 방식).

equity.jsonl 의 각 줄에 그날 들어오거나 나간 돈이 `flow`(달러, 입금 +, 출금 -)로
남아 있으면, 그 돈은 수익으로 치지 않는다. 하루 수익률은
(오늘 평가금액 - 오늘 입출금) / 어제 평가금액 - 1 이고, 누적은 이것을 이어 곱한 값.
대시보드(template.html)도 같은 식으로 계산한다.
"""
from __future__ import annotations


def flow(row: dict) -> float:
    try:
        return float(row.get("flow") or 0)
    except (TypeError, ValueError):
        return 0.0


def growth(rows: list[dict], initial: float | None) -> list[float]:
    """Growth factor after each equity row (1.0 = the starting capital).
    Money moved in or out that day (`flow`) is not counted as gain or loss."""
    out, f = [], 1.0
    prev = float(initial or 0) or None
    for r in rows:
        e = float(r["equity"])
        if prev:
            f *= (e - flow(r)) / prev
        out.append(f)
        prev = e or None
    return out


def total_return(rows: list[dict], initial: float | None, equity_now: float | None = None) -> float | None:
    """Return since the start. `equity_now` (e.g. live prices) replaces the last row's equity."""
    g = growth(rows, initial)
    if not g:
        return None
    if equity_now is None:
        return g[-1] - 1
    last = float(rows[-1]["equity"])
    return g[-1] * equity_now / last - 1 if last else None


def principal(initial: float | None, rows: list[dict]) -> float:
    """Money put in: the starting capital plus deposits minus withdrawals."""
    return float(initial or 0) + sum(flow(r) for r in rows)


def model_growth(series: dict, rows: list[dict], initial: float | None) -> dict[str, float]:
    """Growth factors of the backtest series (dollar values that include the same
    flows) on the days it covers; it starts where the account did."""
    real = dict(zip((r["date"] for r in rows), growth(rows, initial)))
    out, f, prev = {}, None, None
    for r in rows:
        v = series.get(r["date"])
        if v is None:
            continue
        v = float(v)
        f = real[r["date"]] if f is None else f * (v - flow(r)) / prev
        out[r["date"]] = f
        prev = v
    return out
