"""A graphical Gantt of the project registry, rendered without any LLM.

``render_gantt_html`` reads the same schedule view the ``projects_schedule``
tool returns and draws it as one self-contained HTML page (inline SVG, no
scripts, no network): one chart per project, a bar per dated task over its
planned window, a mark on the day it was completed, a line for today. It is
plain code over the Store, so a Claude Code session, the CEO or a scheduled
job can regenerate it as often as they like at no model cost.

    python -m lazytools.projects.gantt --store C:/ProgramData/lazyceo/ceo_simple.sqlite --out gantt.html
"""

from __future__ import annotations

import argparse
import html
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from lazytools.projects import owner as _owner
from lazytools.projects import records as _records
from lazytools.projects import schedule as _schedule

DAY_PX = 22
LABEL_PX = 250
ROW_PX = 26
TOP_PX = 34
DEFAULT_STATUSES = ("open", "paused")

_STATE_TEXT = {
    "unscheduled": "senza date",
    "on_track": "in linea",
    "at_risk": "a rischio",
    "behind": "in ritardo",
    "blocked": "bloccato",
    "ready_to_close": "da chiudere",
    "done": "chiuso",
    "paused": "in pausa",
}


def _day(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def _label(text: str, limit: int = 34) -> str:
    first = " ".join(text.split())
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _task_kind(task: Any, today: date) -> str:
    status = str(task.status)
    if status == "done":
        return "done"
    if status in ("cancelled", "retired"):
        return "cancelled"
    if status == "failed":
        return "late"
    due = _day(task.due_at)
    if due is not None and due < today:
        return "late"
    return "open"


def _drawn_tasks(view: Any, *, unplanned_done: bool) -> list[Any]:
    """Tasks with a planned window; with ``unplanned_done`` also those that only
    have a completion date (one dot each -- on a long project, mostly noise)."""
    return [t for t in view.tasks if t.planned_start_at or t.due_at or (unplanned_done and t.completed_at)]


def _project_svg(view: Any, today: date, *, unplanned_done: bool = False) -> str:
    dated = _drawn_tasks(view, unplanned_done=unplanned_done)
    if not dated:
        return '<p class="muted">Nessun task ha date pianificate.</p>'
    days: list[date] = [today]
    for task in dated:
        days += [d for d in (_day(task.planned_start_at), _day(task.due_at), _day(task.completed_at)) if d is not None]
    target = _day(view.schedule_status.target_completion_at)
    if target is not None:
        days.append(target)
    first, last = min(days) - timedelta(days=1), max(days) + timedelta(days=1)
    span = (last - first).days + 1
    width = LABEL_PX + span * DAY_PX
    height = TOP_PX + len(dated) * ROW_PX + 8

    def x(day: date) -> int:
        return LABEL_PX + (day - first).days * DAY_PX

    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">']
    for offset in range(span):
        day = first + timedelta(days=offset)
        if day.weekday() >= 5:
            parts.append(f'<rect class="weekend" x="{x(day)}" y="{TOP_PX - 6}" width="{DAY_PX}" height="{height - TOP_PX}"/>')
        if day.weekday() == 0 or offset == 0:
            parts.append(f'<line class="grid" x1="{x(day)}" y1="{TOP_PX - 6}" x2="{x(day)}" y2="{height}"/>')
            parts.append(f'<text class="axis" x="{x(day) + 2}" y="16">{day:%d/%m}</text>')
    if target is not None:
        tx = x(target) + DAY_PX
        parts.append(f'<line class="target" x1="{tx}" y1="{TOP_PX - 10}" x2="{tx}" y2="{height}"/>')
        parts.append(f'<text class="target-label" x="{tx + 3}" y="28">target</text>')
    for row, task in enumerate(dated):
        y = TOP_PX + row * ROW_PX
        kind = _task_kind(task, today)
        full = html.escape(" ".join(task.text.split()))
        parts.append(f"<g><title>#{task.task_index} [{html.escape(str(task.status))}] {full}</title>")
        parts.append(f'<text class="label {kind}" x="6" y="{y + 16}">{html.escape(_label(task.text))}</text>')
        start, due = _day(task.planned_start_at), _day(task.due_at)
        if start or due:
            a = start or due
            b = due or start
            assert a is not None and b is not None
            a, b = min(a, b), max(a, b)
            hold = " hold" if task.start_hold and kind != "done" else ""
            parts.append(
                f'<rect class="bar {kind}{hold}" x="{x(a) + 1}" y="{y + 5}" '
                f'width="{(b - a).days * DAY_PX + DAY_PX - 2}" height="{ROW_PX - 10}" rx="4"/>'
            )
        completed = _day(task.completed_at)
        if completed is not None:
            parts.append(f'<circle class="done-mark" cx="{x(completed) + DAY_PX / 2}" cy="{y + ROW_PX / 2}" r="5"/>')
        parts.append("</g>")
    today_x = x(today) + DAY_PX / 2
    parts.append(f'<line class="today" x1="{today_x}" y1="{TOP_PX - 10}" x2="{today_x}" y2="{height}"/>')
    parts.append(f'<text class="today-label" x="{today_x + 3}" y="28">oggi</text>')
    parts.append("</svg>")
    return "".join(parts)


def _project_section(view: Any, owner: str, today: date, *, unplanned_done: bool = False) -> str:
    status = view.schedule_status
    project = view.project
    state = _STATE_TEXT.get(str(status.state), str(status.state))
    target = _day(status.target_completion_at)
    undated = sum(
        1 for t in view.tasks if not (t.planned_start_at or t.due_at or t.completed_at) and str(t.status) not in ("done", "cancelled", "retired")
    )
    hidden = 0 if unplanned_done else sum(1 for t in view.tasks if t.completed_at and not (t.planned_start_at or t.due_at))
    facts = [
        f"{status.done_tasks}/{status.total_tasks} fatti",
        f"{hidden} chiusi senza pianificazione (non disegnati)" if hidden else "",
        f"{len(status.overdue)} scaduti" if status.overdue else "",
        f"target {target:%d/%m}" if target else "",
        f"{undated} aperti senza date" if undated else "",
        f"owner {owner}",
    ]
    pct = round(100 * status.done_tasks / status.total_tasks) if status.total_tasks else 0
    return (
        f'<section><header><h2>{html.escape(project.title)}</h2>'
        f'<span class="badge {html.escape(str(status.state))}">{html.escape(state)}</span></header>'
        f'<div class="progress"><div style="width:{pct}%"></div></div>'
        f'<p class="facts">{" · ".join(html.escape(f) for f in facts if f)}</p>'
        f'<div class="chart">{_project_svg(view, today, unplanned_done=unplanned_done)}</div></section>'
    )


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--weekend:#f1f0ec;
--done:#2f8f5b;--open:#3b6fd8;--late:#d2453b;--cancel:#b8b6ae;--today:#e08a00;--target:#7a5cc9}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;
--muted:#9a998f;--line:#33332f;--weekend:#262624;--done:#4fb57d;--open:#6c96ec;--late:#ec6a5f;--cancel:#5c5b55;
--today:#f2a531;--target:#a68af0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 40px}h1{font-size:22px;margin:0 0 4px}
.sub,.muted,.facts{color:var(--muted);font-size:13px;margin:4px 0}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;color:var(--muted);margin:10px 0 18px}
.legend i{display:inline-block;width:14px;height:10px;border-radius:3px;margin-right:5px;vertical-align:middle}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 14px 10px;margin:0 0 16px}
header{display:flex;gap:10px;align-items:center;justify-content:space-between}h2{font-size:17px;margin:0}
.badge{font-size:12px;padding:2px 9px;border-radius:999px;background:var(--line);white-space:nowrap}
.badge.behind,.badge.blocked{background:var(--late);color:#fff}.badge.at_risk{background:var(--today);color:#fff}
.badge.on_track,.badge.ready_to_close{background:var(--done);color:#fff}
.progress{height:6px;background:var(--line);border-radius:3px;margin:10px 0 4px;overflow:hidden}
.progress div{height:100%;background:var(--done)}
.chart{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:8px}
svg{display:block;font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.axis,.today-label,.target-label{fill:var(--muted);font-size:11px}.today-label{fill:var(--today)}.target-label{fill:var(--target)}
.grid{stroke:var(--line)}.weekend{fill:var(--weekend)}
.today{stroke:var(--today);stroke-width:2}.target{stroke:var(--target);stroke-width:1.5;stroke-dasharray:4 3}
.label{fill:var(--ink)}.label.cancelled{fill:var(--muted);text-decoration:line-through}.label.late{fill:var(--late)}
.bar.done{fill:var(--done);opacity:.85}.bar.open{fill:var(--open)}.bar.late{fill:var(--late)}
.bar.cancelled{fill:var(--cancel)}.bar.hold{fill-opacity:.35;stroke-width:1.5;stroke-dasharray:4 3}
.bar.open.hold{stroke:var(--open)}.bar.late.hold{stroke:var(--late)}
.done-mark{fill:var(--card);stroke:var(--done);stroke-width:2.5}
"""


def render_gantt_html(
    store: Any,
    *,
    statuses: Sequence[str] | None = DEFAULT_STATUSES,
    owners: Iterable[str] | None = None,
    now: datetime | None = None,
    title: str = "Gantt dei progetti",
    unplanned_done: bool = False,
) -> str:
    """The whole registry's Gantt as one self-contained HTML page.

    ``statuses`` limits which projects are drawn (``None`` = every status);
    ``owners`` limits by owner (``None`` = every owner). Tasks closed without
    ever having planned dates are counted, not drawn, unless ``unplanned_done``."""
    moment = now or datetime.now(UTC)
    today = moment.astimezone(UTC).date()
    sections = [
        _project_section(view, project_owner, today, unplanned_done=unplanned_done)
        for view, project_owner in _selected_views(store, statuses=statuses, owners=owners, moment=moment)
    ]
    body = "".join(sections) or '<p class="muted">Nessun progetto da mostrare.</p>'
    legend = (
        '<div class="legend"><span><i style="background:var(--done)"></i>fatto</span>'
        '<span><i style="background:var(--open)"></i>pianificato</span>'
        '<span><i style="background:var(--late)"></i>scaduto / fallito</span>'
        '<span><i style="background:var(--cancel)"></i>annullato</span>'
        '<span><i style="border:1.5px dashed var(--open)"></i>in attesa (hold)</span>'
        '<span>○ giorno di chiusura</span></div>'
    )
    return (
        '<!doctype html><html lang="it"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{html.escape(title)}</h1><p class=\"sub\">Aggiornato {moment:%d/%m/%Y %H:%M} UTC · "
        "tocca una barra per il testo completo del task</p>"
        f"{legend}{body}</main></body></html>"
    )


#: How far the text Gantt reaches either side of today: a chat line has room
#: for about six weeks of one-character days beside a task label.
TEXT_DAYS_BACK = 21
TEXT_DAYS_AHEAD = 14
TEXT_LABEL = 24


def _selected_views(
    store: Any, *, statuses: Sequence[str] | None, owners: Iterable[str] | None, moment: datetime
) -> list[tuple[Any, str]]:
    wanted_owners = set(owners) if owners is not None else None
    views: list[tuple[Any, str]] = []
    for record in _records.list_projects(store):
        if statuses is not None and str(record.status) not in statuses:
            continue
        project_owner = _owner.get_project_owner(store, record.project_id)
        if wanted_owners is not None and project_owner not in wanted_owners:
            continue
        views.append((_schedule.project_schedule_view(store, record.project_id, now=moment), project_owner))
    return views


def render_gantt_text(
    store: Any,
    *,
    statuses: Sequence[str] | None = DEFAULT_STATUSES,
    owners: Iterable[str] | None = None,
    now: datetime | None = None,
    unplanned_done: bool = False,
) -> str:
    """The same Gantt as plain text, one character per day, for a chat or a terminal.

    Covers ``TEXT_DAYS_BACK`` days before today to ``TEXT_DAYS_AHEAD`` after
    (bars outside are clipped). Meant to sit in a monospace block: █ planned
    window of a done task, ✓ completion day, ▓ planned window already past and
    not done, ▒ planned window still ahead, | today."""
    moment = now or datetime.now(UTC)
    today = moment.astimezone(UTC).date()
    first, last = today - timedelta(days=TEXT_DAYS_BACK), today + timedelta(days=TEXT_DAYS_AHEAD)
    span = (last - first).days + 1
    pad = " " * (TEXT_LABEL + 2)
    axis = [" "] * (span + 6)
    for offset in range(span):
        day = first + timedelta(days=offset)
        if day.weekday() == 0:
            for i, ch in enumerate(f"{day:%d/%m}"):
                axis[offset + i] = ch
    lines = [pad + "".join(axis).rstrip()]
    for view, project_owner in _selected_views(store, statuses=statuses, owners=owners, moment=moment):
        status = view.schedule_status
        state = _STATE_TEXT.get(str(status.state), str(status.state))
        target = _day(status.target_completion_at)
        head = f"{view.project.project_id}  ({status.done_tasks}/{status.total_tasks} fatti · {state}"
        head += f" · target {target:%d/%m}" if target else ""
        head += f" · owner {project_owner})" if project_owner != "ceo" else ")"
        lines += ["", head]
        drawn = _drawn_tasks(view, unplanned_done=unplanned_done)
        if not drawn:
            lines.append("  nessun task con date pianificate")
            continue
        for task in drawn:
            kind = _task_kind(task, today)
            start, due, completed = _day(task.planned_start_at), _day(task.due_at), _day(task.completed_at)
            if start or due:
                start, due = min(start or due, due or start), max(start or due, due or start)  # type: ignore[type-var]
            row = []
            for offset in range(span):
                day = first + timedelta(days=offset)
                ch = "|" if day == today else " "
                if start and due and start <= day <= due:
                    ch = "█" if kind == "done" else ("░" if kind == "cancelled" else ("▒" if day >= today else "▓"))
                if completed == day:
                    ch = "✓"
                row.append(ch)
            note = {"done": "fatto", "cancelled": "annullato", "late": "IN RITARDO"}.get(kind, "da fare")
            if task.start_hold and kind not in ("done", "cancelled"):
                note += " · in attesa"
            lines.append(f"  {_label(task.text, TEXT_LABEL):<{TEXT_LABEL}}{''.join(row)} {note}")
    if len(lines) == 1:
        lines.append("nessun progetto da mostrare")
    return "\n".join(lines)


def write_gantt_html(store: Any, out_path: str | Path, **kwargs: Any) -> Path:
    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_gantt_html(store, **kwargs), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lazytools.projects.gantt", description=__doc__.splitlines()[0])
    parser.add_argument("--store", required=True, help="Path of the project Store (SQLite).")
    parser.add_argument("--out", default="gantt.html")
    parser.add_argument("--all-statuses", action="store_true", help="Also draw draft and done projects.")
    parser.add_argument("--owner", action="append", choices=("ceo", "claude", "shared"), help="Repeatable; default every owner.")
    parser.add_argument("--unplanned-done", action="store_true", help="Also draw tasks closed without planned dates.")
    parser.add_argument("--text", action="store_true", help="Also print the plain-text Gantt.")
    args = parser.parse_args(argv)

    from lazybridge import Store

    store = Store(db=args.store)
    path = write_gantt_html(
        store,
        args.out,
        statuses=None if args.all_statuses else DEFAULT_STATUSES,
        owners=args.owner,
        unplanned_done=args.unplanned_done,
    )
    if args.text:
        statuses = None if args.all_statuses else DEFAULT_STATUSES
        print(render_gantt_text(store, statuses=statuses, owners=args.owner, unplanned_done=args.unplanned_done))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
