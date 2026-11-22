"""Bounded, persisted UI snapshots; background refresh never blocks an HTTP read."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import threading
import time

from fastapi.encoders import jsonable_encoder
from ahledger.util import atomic_json


class ReadViews:
    def __init__(self, root, *, limit=32, ttl=5):
        self.root=root/'ui-views-v1';self.root.mkdir(exist_ok=True)
        self.limit,self.ttl=limit,ttl
        self.lock=threading.Lock();self.entries=OrderedDict();self.pending={};self.errors={}
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='cennik-read-view')
        self.closed=False

    def get(self,key,loader,*,fallback=None):
        identity=hashlib.sha256(json.dumps(key,sort_keys=True,default=str).encode()).hexdigest()
        with self.lock:
            entry=self.entries.get(identity)
            if entry is None:
                path=self.root/(identity+'.json')
                try:
                    if path.stat().st_size<=2_000_000:
                        saved=json.loads(path.read_text('utf-8'))
                        if saved.get('version')==1 and isinstance(saved.get('data'),dict) and isinstance(saved.get('at'),(int,float)):
                            entry=saved;self.entries[identity]=entry
                except (OSError,ValueError,TypeError):pass
            stale=not entry or time.time()-entry['at']>self.ttl
            if stale and identity not in self.pending and len(self.pending)<self.limit and not self.closed:
                self.pending[identity]=True
                self.pool.submit(self._refresh,identity,loader)
            if entry:
                self.entries.move_to_end(identity)
                value={**entry['data']}
            else:
                value={**(fallback() if callable(fallback) else fallback or {})}
            value['view_cache']={'available':entry is not None,'refreshing':identity in self.pending,
                'updated_at':datetime.fromtimestamp(entry['at'],UTC).isoformat() if entry else None,
                'stale':stale,'error':self.errors.get(identity)}
            self._trim()
            return value

    def _refresh(self,identity,loader):
        try:
            result=jsonable_encoder(loader())
            value={'version':1,'at':time.time(),'data':result}
            body=json.dumps(value,ensure_ascii=False)
            if len(body.encode())<=2_000_000:
                atomic_json(self.root/(identity+'.json'),value)
            with self.lock:
                self.entries[identity]=value;self.entries.move_to_end(identity)
                self.errors.pop(identity,None)
                self._trim()
        except Exception as error:
            with self.lock:self.errors[identity]=str(error)[:400] if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__
        finally:
            with self.lock:self.pending.pop(identity,None)

    def _trim(self):
        while len(self.entries)>self.limit:
            identity,_=self.entries.popitem(last=False)
            (self.root/(identity+'.json')).unlink(missing_ok=True)
            self.errors.pop(identity,None)
        paths=sorted(self.root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for path in paths[self.limit:]:path.unlink(missing_ok=True)

    def close(self):
        with self.lock:self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)

# EOF
