"""Render the analyst-skills flow as a self-contained HTML diagram.

Two layers, both data-driven:

* **The contract graph** — from each :class:`~lazytools.skills.Skill`'s
  ``reads``/``writes``: the specialists as a pipeline and the shared blackboard
  as the hub, with every handle drawn as an edge (who produces it, who consumes
  it). This is the "how it works" picture, independent of any run.
* **The real run** — from a shared :class:`~lazybridge.Session`'s event log
  (``analyst_events.db``): the order the specialists actually executed, their
  durations, and the handles each published.

Usage::

    C:\\ProgramData\\spyder-6\\python.exe examples\\analyst_viz.py \\
        ..\\reports_demo\\analyst_events.db  ..\\reports_demo\\analyst_flow.html
"""

from __future__ import annotations

import json
import sqlite3
import sys
from html import escape
from pathlib import Path

LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(LAZYTOOLS_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(LAZYTOOLS_ROOT / "src"))

from lazytools.skills import SKILLS

ORDER = ["market_data", "financials", "stats", "regime", "report"]


def _run_facts(events_db: str) -> dict[str, dict]:
    """Per-specialist: execution order, duration (ms), and published handles."""
    facts: dict[str, dict] = {}
    if not Path(events_db).exists():
        return facts
    con = sqlite3.connect(events_db)
    rows = con.execute("SELECT event_type, payload, ts FROM events ORDER BY id").fetchall()
    con.close()
    seq = 0
    for event_type, payload_json, _ts in rows:
        try:
            p = json.loads(payload_json) if payload_json else {}
        except Exception:
            p = {}
        agent = p.get("agent_name")
        if event_type == "agent_start" and agent in ORDER and agent not in facts:
            seq += 1
            facts[agent] = {"order": seq, "handles": [], "latency_ms": None}
        elif event_type == "agent_finish" and agent in facts:
            facts[agent]["latency_ms"] = p.get("latency_ms")
        elif event_type == "tool_call" and p.get("step") == "publish" and agent in facts:
            task = str(p.get("task", ""))
            facts[agent]["handles"] = [w for w in _skill(agent).writes if w in task] or list(_skill(agent).writes)
    return facts


def _skill(name: str):
    return next(s for s in SKILLS if s.name == name)


# --------------------------------------------------------------------------- #
# SVG layout
# --------------------------------------------------------------------------- #
_CARD_W, _CARD_H, _GAP, _X0, _Y_CARD = 210, 96, 34, 40, 70
_Y_BOARD = 340


def _svg(facts: dict[str, dict]) -> str:
    n = len(ORDER)
    width = _X0 * 2 + n * _CARD_W + (n - 1) * _GAP
    board_x, board_w = _X0, width - 2 * _X0
    parts: list[str] = []

    # blackboard hub
    parts.append(
        f'<rect x="{board_x}" y="{_Y_BOARD}" width="{board_w}" height="120" rx="14" '
        f'class="board"/>'
        f'<text x="{board_x + 16}" y="{_Y_BOARD + 26}" class="board-label">SHARED BLACKBOARD '
        f'(lazybridge Store) — only short handles cross it</text>'
    )

    handle_x: dict[str, float] = {}
    for i, name in enumerate(ORDER):
        sk = _skill(name)
        cx = _X0 + i * (_CARD_W + _GAP)
        f = facts.get(name, {})
        done = bool(f)
        order = f.get("order")
        lat = f.get("latency_ms")
        badge = f"#{order}" if order else "—"
        secs = f"{lat / 1000:.0f}s" if lat else ""
        # specialist card
        parts.append(
            f'<g class="card {"done" if done else "idle"}">'
            f'<rect x="{cx}" y="{_Y_CARD}" width="{_CARD_W}" height="{_CARD_H}" rx="12"/>'
            f'<text x="{cx + 14}" y="{_Y_CARD + 26}" class="card-name">{escape(name)}</text>'
            f'<text x="{cx + _CARD_W - 14}" y="{_Y_CARD + 26}" class="card-badge" text-anchor="end">{badge} {secs}</text>'
            f'<text x="{cx + 14}" y="{_Y_CARD + 50}" class="card-sub">{escape(sk.summary)}</text>'
            f'<text x="{cx + 14}" y="{_Y_CARD + 72}" class="card-io">reads: {escape(", ".join(sk.reads) or "—")}</text>'
            f'<text x="{cx + 14}" y="{_Y_CARD + 88}" class="card-io">writes: {escape(", ".join(sk.writes) or "—")}</text>'
            f"</g>"
        )
        # handle chips under their producer, on the board
        cw = sk.writes
        for j, h in enumerate(cw):
            hx = cx + 8 + j * (_CARD_W - 16) / max(len(cw), 1)
            handle_x[h] = hx + 6
            produced = h in f.get("handles", [])
            parts.append(
                f'<rect x="{hx}" y="{_Y_BOARD + 44}" width="{(_CARD_W - 16) / max(len(cw), 1) - 8:.0f}" '
                f'height="24" rx="12" class="chip {"chip-on" if produced else "chip-off"}"/>'
                f'<text x="{hx + 8}" y="{_Y_BOARD + 60}" class="chip-txt">{escape(h)}</text>'
            )
            # write edge: card -> chip
            parts.append(
                f'<path d="M {cx + _CARD_W / 2:.0f} {_Y_CARD + _CARD_H} '
                f'C {cx + _CARD_W / 2:.0f} {_Y_BOARD - 20}, {hx + 20:.0f} {_Y_BOARD - 10}, '
                f'{hx + 20:.0f} {_Y_BOARD + 44}" class="edge-w {"on" if produced else ""}"/>'
            )

    # read edges: chip -> consumer card (dashed, upward)
    for name in ORDER:
        sk = _skill(name)
        cx = _X0 + ORDER.index(name) * (_CARD_W + _GAP)
        for h in sk.reads:
            if h in handle_x:
                hx = handle_x[h]
                parts.append(
                    f'<path d="M {hx:.0f} {_Y_BOARD + 44} '
                    f'C {hx:.0f} {_Y_BOARD - 30}, {cx + _CARD_W / 2:.0f} {_Y_BOARD - 30}, '
                    f'{cx + _CARD_W / 2:.0f} {_Y_CARD + _CARD_H}" class="edge-r"/>'
                )

    return f'<svg viewBox="0 0 {width} 480" xmlns="http://www.w3.org/2000/svg" class="flow">{"".join(parts)}</svg>'


def build_html(events_db: str) -> str:
    facts = _run_facts(events_db)
    ran = [n for n in ORDER if n in facts]
    subtitle = (
        f"Real run — {len(ran)}/{len(ORDER)} specialists: " + " → ".join(ran)
        if ran
        else "Contract graph (no run events found)"
    )
    return f"""<title>Analyst skills — flow</title>
<style>
:root {{ --bg:#f7f8fa; --fg:#1b2030; --muted:#6b7280; --card:#ffffff; --line:#c9ced8;
  --accent:#3b6ea5; --on:#2e9e6b; --board:#eef1f6; --chip:#e4e8ef; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1420; --fg:#e6e9f0; --muted:#94a0b4;
  --card:#1a2130; --line:#333c4e; --accent:#6ea8dc; --on:#54c48c; --board:#151b28; --chip:#232c3d; }} }}
:root[data-theme="light"] {{ --bg:#f7f8fa; --fg:#1b2030; --card:#fff; --line:#c9ced8; --board:#eef1f6; --chip:#e4e8ef; --muted:#6b7280; }}
:root[data-theme="dark"] {{ --bg:#0f1420; --fg:#e6e9f0; --card:#1a2130; --line:#333c4e; --board:#151b28; --chip:#232c3d; --muted:#94a0b4; }}
body {{ margin:0; }} .wrap {{ max-width:1120px; margin:0 auto; padding:28px 20px 48px;
  font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; color:var(--fg); background:var(--bg); }}
h1 {{ font-size:22px; margin:0 0 2px; letter-spacing:-.01em; }}
.sub {{ color:var(--muted); font-size:13px; margin:0 0 18px; }}
.scroll {{ overflow-x:auto; border:1px solid var(--line); border-radius:14px; background:var(--card); }}
svg.flow {{ display:block; min-width:1040px; width:100%; }}
.board {{ fill:var(--board); stroke:var(--line); }}
.board-label {{ fill:var(--muted); font-size:12px; font-weight:600; letter-spacing:.02em; }}
.card rect {{ fill:var(--card); stroke:var(--line); stroke-width:1.5; }}
.card.done rect {{ stroke:var(--on); }}
.card-name {{ fill:var(--fg); font-size:15px; font-weight:700; }}
.card-badge {{ fill:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }}
.card-sub {{ fill:var(--muted); font-size:11px; }}
.card-io {{ fill:var(--muted); font-size:10.5px; font-family:ui-monospace,monospace; }}
.chip {{ stroke:var(--line); }} .chip-off {{ fill:var(--chip); }} .chip-on {{ fill:var(--on); opacity:.22; stroke:var(--on); }}
.chip-txt {{ fill:var(--fg); font-size:10.5px; font-family:ui-monospace,monospace; }}
.edge-w {{ fill:none; stroke:var(--line); stroke-width:1.5; }} .edge-w.on {{ stroke:var(--on); stroke-width:2; }}
.edge-r {{ fill:none; stroke:var(--accent); stroke-width:1.3; stroke-dasharray:4 3; opacity:.75; }}
.legend {{ display:flex; gap:22px; flex-wrap:wrap; margin-top:16px; font-size:12.5px; color:var(--muted); }}
.legend b {{ color:var(--fg); }} .k {{ display:inline-block; width:22px; height:0; border-top:2px solid; vertical-align:middle; margin-right:6px; }}
.orch {{ margin-top:22px; display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }}
.orch div {{ border:1px solid var(--line); border-radius:12px; padding:12px 14px; background:var(--card); }}
.orch h3 {{ margin:0 0 4px; font-size:14px; }} .orch p {{ margin:0; font-size:12.5px; color:var(--muted); }}
</style>
<div class="wrap">
  <h1>Analyst skills — specialist agents over a shared blackboard</h1>
  <p class="sub">{escape(subtitle)}</p>
  <div class="scroll">{_svg(facts)}</div>
  <div class="legend">
    <span><span class="k" style="border-color:var(--line)"></span>writes a handle → blackboard</span>
    <span><span class="k" style="border-color:var(--accent);border-top-style:dashed"></span>reads a handle ← blackboard</span>
    <span><span class="k" style="border-color:var(--on)"></span>produced in this run</span>
    <span>Heavy data stays in the depot/DB/disk; <b>only short handles cross the blackboard.</b></span>
  </div>
  <div class="orch">
    <div><h3>plan</h3><p>Deterministic pipeline: run the specialists in fixed dependency order. Reproducible, checkpointable.</p></div>
    <div><h3>blackboard</h3><p>A planner keeps a flat to-do list and picks the next ready specialist by its description.</p></div>
    <div><h3>replan</h3><p>Plan → execute → observe → replan: adapts on failure (e.g. register a new ticker after an error).</p></div>
  </div>
</div>
"""


def main() -> None:
    events_db = sys.argv[1] if len(sys.argv) > 1 else str(LAZYTOOLS_ROOT.parent / "reports_demo" / "analyst_events.db")
    out_html = sys.argv[2] if len(sys.argv) > 2 else str(LAZYTOOLS_ROOT.parent / "reports_demo" / "analyst_flow.html")
    Path(out_html).write_text(build_html(events_db), encoding="utf-8")
    print(f"wrote {out_html}")


if __name__ == "__main__":
    main()
