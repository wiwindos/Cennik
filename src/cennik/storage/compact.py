"""Offline selective SQLite rebuild with an untouched source and verified output."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

from cennik.core.diagnostics import summarize_run
from cennik.core.models import Run
from .retention import protected_opportunities


def quoted(name):
    return '"'+name.replace('"', '""')+'"'


def table_digest(db, schema, table):
    fields = db.execute('PRAGMA '+schema+'.table_info('+quoted(table)+')').fetchall()
    primary = [row[1] for row in sorted(fields,key=lambda row:row[5]) if row[5]]
    order = ','.join(map(quoted,primary)) if primary else 'rowid'
    digest, count = hashlib.sha256(), 0
    for row in db.execute('SELECT * FROM '+schema+'.'+quoted(table)+' ORDER BY '+order):
        value = repr(tuple(row)).encode('utf-8')
        digest.update(len(value).to_bytes(8,'big'));digest.update(value);count += 1
    return {'rows':count,'sha256':digest.hexdigest()}


def rebuild_database(source: Path, target: Path, *, progress=None, max_bytes=256_000_000):
    source, target = source.resolve(), target.resolve()
    if not source.is_file() or target.exists() or source == target or target.parent != source.parent:
        raise ValueError('Нужны существующая БД и новый файл в той же папке')
    free = shutil.disk_usage(source.parent).free
    if free < max_bytes+32_000_000:
        raise ValueError('Недостаточно места для проверенной компактной копии')
    original_stat=source.stat()
    original_identity=(original_stat.st_ino,original_stat.st_size,original_stat.st_mtime_ns)
    report = {'source_bytes':original_stat.st_size,'source_identity':original_identity,'state':'copying','tables':{}}
    progress = progress or (lambda report:None)
    db = sqlite3.connect(target,uri=True,timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA cache_size=-8192')
        db.execute('ATTACH DATABASE ? AS original',[source.as_uri()+'?mode=ro'])
        schema = db.execute("SELECT type,name,sql FROM original.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        names = [row['name'] for row in schema if row['type']=='table']
        if not {'runs','opportunities','published'} <= set(names):
            raise ValueError('Это не база аналитики Cennik')

        def budget():
            return int(target.stat().st_size>max_bytes or shutil.disk_usage(source.parent).free<16_000_000)
        db.set_progress_handler(budget,10000)
        db.execute('BEGIN')
        for row in schema:
            if row['type']=='table':
                db.execute(row['sql'])
        if 'results_retained' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
            db.execute('ALTER TABLE runs ADD COLUMN results_retained INTEGER NOT NULL DEFAULT 1')
        # Copy every non-analytic record verbatim, including tasks and accounting.
        for table in names:
            if table in {'runs','opportunities'}:
                continue
            db.execute('INSERT INTO main.'+quoted(table)+' SELECT * FROM original.'+quoted(table))
            report['phase'] = 'copy:'+table;progress(report)
        published = {row[0] for row in db.execute('SELECT run_id FROM published')}
        report['opportunities_before']=db.execute('SELECT count(*) FROM original.opportunities').fetchone()[0]
        protected = protected_opportunities(db)
        old_columns = {row['name'] for row in db.execute('PRAGMA original.table_info(runs)')}
        summary_column = 'summary' if 'summary' in old_columns else 'NULL AS summary'
        records = db.execute('SELECT run_id,market,fingerprint,status,started_at,'+summary_column+' FROM original.runs')
        run_count = 0
        for row in records:
            current = row['run_id'] in published
            summary = row['summary']
            payload = db.execute('SELECT payload FROM original.runs WHERE run_id=?',[row['run_id']]).fetchone()[0] if current or not summary else None
            if not summary:
                run = Run.model_validate_json(payload)
                signals = {r[0]:r[1] for r in db.execute('SELECT strategy,count(*) FROM original.opportunities WHERE run_id=? GROUP BY strategy',[row['run_id']])}
                summary = summarize_run(run,signals=signals).model_dump_json()
            checked = Run.model_validate_json(summary)
            status = row['status']
            if status=='running':
                status = 'interrupted'
                checked = checked.model_copy(update={'status':status,'error':'Расчёт прерван обслуживанием базы'})
                summary = checked.model_dump_json()
                current = False
            db.execute('INSERT INTO runs(run_id,market,fingerprint,status,started_at,payload,summary,results_retained) VALUES(?,?,?,?,?,?,?,?)',
                [row['run_id'],row['market'],row['fingerprint'],status,row['started_at'],payload if current else summary,summary,int(current)])
            run_count += 1
            if run_count%100==0:
                report.update(phase='copy:runs',runs_copied=run_count);progress(report)
        expected = 0
        for identity in published:
            expected += db.execute('SELECT count(*) FROM original.opportunities WHERE run_id=?',[identity]).fetchone()[0]
            db.execute('INSERT INTO main.opportunities SELECT * FROM original.opportunities WHERE run_id=?',[identity])
        referenced = 0
        for identity in protected:
            old = db.execute('SELECT run_id FROM original.opportunities WHERE id=?',[identity]).fetchone()
            if old and old[0] not in published:
                expected += 1;referenced += 1
                db.execute('INSERT INTO main.opportunities SELECT * FROM original.opportunities WHERE id=?',[identity])
        if db.execute('SELECT count(*) FROM opportunities').fetchone()[0] != expected:
            raise RuntimeError('Количество сохранённых возможностей не совпало')
        report.update(phase='indexes',runs_copied=run_count,opportunities_kept=expected,referenced_opportunities=referenced)
        progress(report)
        for row in schema:
            if row['type']!='table':
                db.execute(row['sql'])
        # Verify complete, ordered contents of every unaffected table.
        for table in names:
            if table in {'runs','opportunities'}:
                continue
            before = table_digest(db,'original',table)
            after = table_digest(db,'main',table)
            if before != after:
                raise RuntimeError('Не совпали сохранённые данные: '+table)
            report['tables'][table] = before
            report['phase'] = 'verify:'+table;progress(report)
        if run_count != db.execute('SELECT count(*) FROM original.runs').fetchone()[0]:
            raise RuntimeError('Не совпало количество отчётов расчётов')
        report['phase']='integrity:main';progress(report)
        if db.execute('PRAGMA main.foreign_key_check').fetchall():
            raise RuntimeError('Нарушены связи SQLite')
        if [row[0] for row in db.execute('PRAGMA main.integrity_check')] != ['ok']:
            raise RuntimeError('Компактная БД не прошла проверку целостности')
        checked_stat=source.stat()
        if (checked_stat.st_ino,checked_stat.st_size,checked_stat.st_mtime_ns)!=original_identity:
            raise RuntimeError('Исходная БД изменилась во время обслуживания')
        db.commit()
        db.execute('DETACH DATABASE original')
        report.update(state='verified',phase='ready',target_bytes=target.stat().st_size)
        progress(report)
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--checkpoint',action='store_true',help='Only after the application has stopped')
    args=parser.parse_args()
    def progress(report):
        temporary=args.report.with_suffix('.tmp')
        temporary.write_text(json.dumps({**report,'updated_at':time.time()},ensure_ascii=False,indent=2),'utf-8')
        temporary.replace(args.report)
    try:
        if args.checkpoint:
            with sqlite3.connect(args.source.resolve().as_uri()+'?mode=rw',uri=True,timeout=10) as checkpoint:
                result=checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if result[0]:
                    raise RuntimeError('SQLite ещё используется; checkpoint заблокирован')
        result=rebuild_database(args.source,args.target,progress=progress)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as error:
        try:
            progress({'state':'failed','error':str(error)[:1000]})
        except OSError:
            pass  # Keep the original failure visible if the report path is unwritable.
        raise


if __name__=='__main__':main()
# EOF
