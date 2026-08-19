"""On-demand charts from market-data-hub series — the ``chart:`` scheme.

Turns any series the hub can extract (prices, FRED macro, crypto, factors,
custom) into a PNG line chart for a report figure. Split in two so each half
stays testable alone:

* :func:`render_series_png` — pure renderer: DataFrame in, PNG bytes out.
  Headless by construction (``FigureCanvasAgg`` directly, no pyplot, no
  global backend state), deterministic for a given frame.
* :func:`chart_series` — fetch via ``market_data_hub.extract.extract_series``
  (lazy import, ImportError with install hint if absent) then render.

:func:`parse_chart_spec` decodes the ``chart:<spec>`` artifact-ref key — a
querystring, chosen because it is readable, extensible and stdlib-parseable:
``symbols=SPY,^VIX&start=2020-01-01&frequency=W&transform=log_return``.

Requires matplotlib (and the hub's pandas) — install extra
``lazytoolkit[charts]``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

#: Spec fields accepted by :func:`parse_chart_spec`, all optional but
#: ``symbols``; every value is a string (``symbols`` comma-separated).
_SPEC_FIELDS = {"symbols", "start", "end", "domain", "field", "transform", "frequency", "title"}


def parse_chart_spec(spec: str) -> dict[str, Any]:
    """Decode a ``chart:`` key (querystring) into :func:`chart_series` kwargs."""
    raw = parse_qs(spec, keep_blank_values=False, strict_parsing=False)
    unknown = set(raw) - _SPEC_FIELDS
    if unknown:
        raise ValueError(
            f"unknown chart spec field(s) {sorted(unknown)}; allowed: {sorted(_SPEC_FIELDS)}"
        )
    if "symbols" not in raw:
        raise ValueError(f"chart spec must carry 'symbols=...': {spec!r}")
    out: dict[str, Any] = {k: v[-1] for k, v in raw.items()}
    out["symbols"] = [s for s in out["symbols"].split(",") if s]
    return out


def render_series_png(
    df: Any,
    *,
    title: str = "",
    width_in: float = 10.0,
    height_in: float = 5.0,
    dpi: int = 120,
) -> bytes:
    """Render a wide DataFrame (DatetimeIndex × one column per series) to PNG."""
    import io

    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise ImportError(
            'report charts require matplotlib: pip install "lazytoolkit[charts] @ git+https://github.com/selvaz/LazyTools.git"'
        ) from exc

    if df.empty:
        raise ValueError("cannot chart an empty frame (no rows for the requested window)")

    fig = Figure(figsize=(width_in, height_in), dpi=dpi)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    for col in df.columns:
        ax.plot(df.index, df[col], linewidth=1.2, label=str(col))
    if title:
        ax.set_title(title)
    if len(df.columns) > 1:
        ax.legend(loc="best", fontsize="small")
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def chart_series(
    symbols: list[str] | str,
    start: str | None = None,
    end: str | None = None,
    *,
    domain: str = "prices",
    field: str = "adj_close",
    transform: str = "level",
    frequency: str | None = None,
    title: str = "",
    db_path: str | None = None,
) -> bytes:
    """Chart any hub series as PNG bytes (see hub ``extract_series`` for args)."""
    try:
        from market_data_hub import extract
    except ImportError as exc:  # pragma: no cover - exercised without the package
        raise ImportError(
            "chart_series requires market-data-hub: pip install "
            "'market-data-hub @ git+https://github.com/selvaz/market-data-hub.git'"
        ) from exc

    df, _meta = extract.extract_series(
        symbols,
        start,
        end,
        domain=domain,
        field=field,
        transform=transform,
        frequency=frequency,
        db_path=db_path,
    )
    return render_series_png(df, title=title)
