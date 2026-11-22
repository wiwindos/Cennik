"""Explicit, bounded analysis outside the ingestion executor and live DuckDB."""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime,UTC
from pathlib import Path
from uuid import uuid4
from typing import Annotated

from pydantic import Field
from ahledger.util import atomic_json
from cennik.core.models import Model,Opportunity,Run
from cennik.core.calculation import HEAVY,config_key

class ExtendedRequest(Model):
    profession:str=Field(default='',max_length=80)
    item_ids:list[Annotated[int,Field(gt=0,lt=2**31,strict=True)]]=Field(default_factory=list,max_length=100)

class ExtendedAnalytics:
    memory_limit=384*1024*1024
    def __init__(self,workspace):
        self.w=workspace;self.root=workspace.root/'extended-analysis'
        self.root.mkdir(exist_ok=True);self.lock=threading.Lock();self.stop=threading.Event()
        self.thread=None;self.process=None;self.state=self.read(self.root/'state.json',{'state':'idle'})
        if self.state.get('state') in {'preparing','running'}:
            self.state={**self.state,'state':'interrupted','message':'Расчёт прерван перезапуском. Запустите снова.'}
            atomic_json(self.root/'state.json',self.state)
    @staticmethod
    def read(path,default):
        try:return json.loads(path.read_text('utf-8'))
        except (OSError,ValueError):return default
    def update(self,**values):
        with self.lock:
            self.state={**self.state,**values};atomic_json(self.root/'state.json',self.state)
    def status(self):
        with self.lock:state=dict(self.state)
        active=state.get('state') in {'preparing','running'}
        state['active']=active
        if active:
            state['elapsed_seconds']=max(0,int(time.time()-state['started_at']))
            state['progress']=self.read(self.root/'progress.json',{})
        run=self.w.store.latest(self.w.market_key,published=True,summary=True)
        evidence=run.diagnostics.get('extended_analysis') if run else None
        state['published']=evidence
        state['stale']=bool(evidence and (self.w.recalculate_pending.is_set() or
            evidence['generation']!=run.generation or evidence['config']!=config_key(self.w.effective_config())))
        state['professions']=self.w.settings().professions
        return state
    def start(self,request):
        if any(type(i) is not int or not 0<i<2**31 for i in request.item_ids):raise ValueError('Неверный ID предмета')
        config=self.w.effective_config()
        if not HEAVY.intersection(config.enabled_strategies):raise ValueError('В настройках отключены цепочки и крафт под распыление')
        with self.lock:
            if self.thread and self.thread.is_alive():raise RuntimeError('Расширенная аналитика уже выполняется')
            if self.stop.is_set():raise RuntimeError('Сервер останавливается')
            self.state={'state':'preparing','id':uuid4().hex,'started_at':time.time(),
                'selection':request.model_dump(),'message':'Готовим срез данных. Сбор и обмен продолжаются.'}
            atomic_json(self.root/'state.json',self.state)
            self.thread=threading.Thread(target=self.execute,args=(request,),name='cennik-extended-control',daemon=True)
            self.thread.start()
        return self.status()
    def snapshot(self,request):
        w=self.w;now=datetime.now(UTC);config=w.effective_config()
        with w.market.lock:
            generation=w.analytics.generation()
            versions={s.id:s.version for s in w.analytics.registry.selected(config.enabled_strategies)}
            run=Run(run_id=uuid4().hex,market=w.market_key,started_at=now,as_of=w.evaluation_now(),
                data_as_of=now if w.development_clock() else None,development_mode=bool(w.development_clock()),
                generation=generation,config=config,strategy_versions=versions)
            with w.provider(w.market.path,w.market_key,run.as_of,**({'data_as_of':now} if run.development_mode else {})) as p:
                prices=p.current_prices();items=p.items();recipes=p.recipes();quality=p.quality()
                if not any(v.min_copper and v.min_copper>0 for v in prices.values()):raise ValueError('Нужны цены рынка перед расширенным расчётом')
                quality['metadata']=[{k:r.get(k) for k in ('dataset','item_id','status','warnings','gaps')} for r in quality.get('metadata',[])]
                sales=p.sales_history(config.craft_sales_window_days) if config.craft_sales_window_days else {}
                data={'market':w.market_key,'as_of':run.as_of.isoformat(),'run':run.model_dump(mode='json'),
                    'selection':request.model_dump(),'strategies':sorted(HEAVY.intersection(config.enabled_strategies)),
                    'prices':{i:v.model_dump(mode='json') for i,v in prices.items()},
                    'items':{i:v.model_dump(mode='json') for i,v in items.items()},
                    'recipes':[r.model_dump(mode='json') for r in recipes],'quality':quality,
                    'sales_history':{i:[v.model_dump(mode='json') for v in values] for i,values in sales.items()},
                    'rules':w.analytics.disenchant.snapshot().model_dump(mode='json')}
        path=self.root/'input.json';atomic_json(path,data)
        if path.stat().st_size>48_000_000:raise ValueError('Срез превышает 48 МБ. Уменьшите период статистики продаж.')
        return run
    @staticmethod
    def rss(pid):
        if sys.platform.startswith('linux'):
            try:
                for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                    if line.startswith('VmRSS:'):return int(line.split()[1])*1024
            except OSError:pass
        elif os.name=='nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(k,ctypes.c_size_t) for k in ('peak','rss','paged_peak','paged','nonpaged_peak','nonpaged','pagefile','pagefile_peak')]
            ctypes.windll.kernel32.OpenProcess.restype=wintypes.HANDLE
            ctypes.windll.psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD]
            ctypes.windll.kernel32.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=ctypes.windll.kernel32.OpenProcess(0x410,False,pid)
            if handle:
                try:
                    value=Counters();value.cb=ctypes.sizeof(value)
                    if ctypes.windll.psapi.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb):return value.rss
                finally:ctypes.windll.kernel32.CloseHandle(handle)
        return 0
    def memory_pressure(self):
        root=Path('/sys/fs/cgroup')
        try:
            limit=(root/'memory.max').read_text().strip()
            return limit!='max' and int((root/'memory.current').read_text())>int(limit)-96*1024*1024
        except (OSError,ValueError):return False
    def execute(self,request):
        try:
            run=self.snapshot(request)
            if self.stop.is_set():raise RuntimeError('Расчёт прерван остановкой сервера')
            self.update(state='running',message='Идёт расширенная аналитика. Можно работать в других разделах.')
            atomic_json(self.root/'progress.json',{})
            with (self.root/'worker.log').open('wb') as log:
                self.process=subprocess.Popen([sys.executable,'-m','cennik.workspace.extended_worker',
                    '--snapshot',str(self.root/'input.json'),'--output',str(self.root/'result.json'),
                    '--progress',str(self.root/'progress.json')],stdout=log,stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                peak=0;started=time.monotonic()
                while self.process.poll() is None:
                    rss=self.rss(self.process.pid);peak=max(peak,rss)
                    problem='Расчёт остановлен: недостаточно свободной памяти. Прежние результаты сохранены.' if rss>self.memory_limit or self.memory_pressure() else 'Расчёт превысил лимит 20 минут' if time.monotonic()-started>1200 else 'Расчёт прерван остановкой сервера' if self.stop.is_set() else None
                    if problem:
                        self.process.terminate()
                        try:self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
                        raise RuntimeError(problem)
                    self.stop.wait(.25)
                if self.process.returncode:
                    detail=self.read(self.root/'progress.json',{})
                    raise RuntimeError('Расширенный расчёт не завершён: '+detail.get('error','ошибка процесса'))
            output=self.root/'result.json'
            if output.stat().st_size>32_000_000:raise ValueError('Результат превышает 32 МБ. Выберите профессию или предметы.')
            result=json.loads(output.read_text('utf-8'))
            if config_key(self.w.effective_config())!=config_key(run.config):raise ValueError('Настройки изменились во время расчёта. Запустите его снова.')
            evidence={'generation':run.generation,'config':config_key(run.config),
                'computed_at':run.as_of.isoformat(),'selection':request.model_dump()}
            rows=[Opportunity.model_validate(v) for v in result['items']]
            if any(o.run_id!=run.run_id or o.market!=run.market or o.strategy_id not in HEAVY for o in rows):
                raise ValueError('Процесс вернул результат вне запрошенного рынка или расчёта')
            for item in rows:item.details['extended_analysis']=evidence
            # Keep the latest automatic publication when prices changed during the job.
            latest=self.w.store.latest(self.w.market_key,published=True,summary=True)
            if latest:run.generation=latest.generation
            run.status='completed';run.finished_at=datetime.now(UTC)
            run.diagnostics={'calculation_scope':'extended','extended_analysis':evidence,
                'rejections':result['rejections'],'extended_seconds':result['elapsed_seconds']}
            self.w.store.begin(run,'extended-'+run.run_id)
            self.w.store.finish(run,rows)
            self.update(state='completed',finished_at=time.time(),count=len(rows),peak_memory_bytes=peak,
                message=f'Расширенная аналитика готова. Найдено: {len(rows)}.')
        except Exception as error:
            self.w.log.exception('Extended analytics failed')
            self.update(state='interrupted' if self.stop.is_set() else 'failed',finished_at=time.time(),
                message=str(error)[:400] if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__)
        finally:
            self.process=None
            (self.root/'input.json').unlink(missing_ok=True)
            (self.root/'result.json').unlink(missing_ok=True)
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join()

# EOF
