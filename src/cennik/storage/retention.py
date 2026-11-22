"""Retain the published result and opportunities referenced by durable tasks."""
from __future__ import annotations

import json

from cennik.core.diagnostics import summarize_run
from cennik.core.models import Run


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def protected_opportunities(db):
    known = tables(db)
    result = set()
    if 'addon_tasks' in known:
        result.update(row[0] for row in db.execute('SELECT opportunity_id FROM addon_tasks'))

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'opportunity_id', 'original_opportunity_id'} and isinstance(child, str):
                    result.add(child)
                elif key == 'opportunity_ids' and isinstance(child, list):
                    result.update(v for v in child if isinstance(v, str))
                elif key == 'opportunity' and isinstance(child, dict) and isinstance(child.get('id'), str):
                    result.add(child['id'])
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for table in ('workspace_plans', 'addon_tasks', 'addon_packages'):
        if table in known:
            for row in db.execute('SELECT payload FROM '+table):
                visit(json.loads(row[0]))
    return result


def compact_run(payload):
    run = Run.model_validate_json(payload)
    return summarize_run(run, signals=run.diagnostics.get('strategy_signals')).model_dump_json()


def rotate_results(db):
    """Called inside the publication transaction; never changes other tables."""
    protected = protected_opportunities(db)
    db.execute('CREATE TEMP TABLE IF NOT EXISTS retention_protected(id TEXT PRIMARY KEY)')
    db.execute('DELETE FROM retention_protected')
    db.executemany('INSERT INTO retention_protected VALUES(?)', [(value,) for value in protected])
    deleted = db.execute("""DELETE FROM opportunities WHERE run_id NOT IN (SELECT run_id FROM published)
        AND id NOT IN (SELECT id FROM retention_protected)""").rowcount
    # Only formerly full results need work. Historical rows remain small forever.
    retired = db.execute("""SELECT run_id,summary FROM runs WHERE results_retained=1
        AND status<>'running' AND run_id NOT IN (SELECT run_id FROM published)""").fetchall()
    for row in retired:
        summary = row[1] or compact_run(db.execute('SELECT payload FROM runs WHERE run_id=?',[row[0]]).fetchone()[0])
        db.execute('UPDATE runs SET payload=?,summary=?,results_retained=0 WHERE run_id=?', [summary,summary,row[0]])
    return {'opportunities_removed': deleted, 'runs_compacted': len(retired), 'protected_opportunities': len(protected)}

# EOF
