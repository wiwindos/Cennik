"""Disposable analysis process. Reads an immutable input, never the live database."""
import argparse
import json
import time
from datetime import datetime,UTC
from pathlib import Path

from ahledger.util import atomic_json
from cennik.core.models import Item,PriceObservation,Recipe,Run
from cennik.core.interfaces import EvaluationContext
from cennik.engines.disenchant import DisenchantRules
from cennik.engines.prices import PriceEngine
from cennik.services.analytics import AnalyticsService
from cennik.strategies import default_registry

class SnapshotProvider:
    def __init__(self,data):
        self.market=data['market'];self.as_of=datetime.fromisoformat(data['as_of'])
        self.data=data
        self.prices={int(k):PriceObservation.model_validate(v) for k,v in data['prices'].items()}
        self.catalog={int(k):Item.model_validate(v) for k,v in data['items'].items()}
        self.recipe_rows=[Recipe.model_validate(v) for v in data['recipes']]
    def current_prices(self):return self.prices
    def items(self):return self.catalog
    def recipes(self):return self.recipe_rows
    def quality(self):return self.data['quality']
    def sales_history(self,days):
        return {int(k):[PriceObservation.model_validate(v) for v in rows] for k,rows in self.data['sales_history'].items()}
    def historical_prices(self,days):return {}
    def source_reference(self,item_id):return None

def calculate(snapshot,output,progress):
    data=json.loads(snapshot.read_text('utf-8'));run=Run.model_validate(data['run'])
    provider=SnapshotProvider(data);prices=PriceEngine(provider,run.config)
    context=EvaluationContext(provider,run.config,run.run_id,run.started_at,prices,provider.items(),
        disenchant=DisenchantRules.model_validate(data['rules']),selection=data['selection'])
    for key in ('prices','items','recipes','rules','run'):data.pop(key,None)
    unique={};registry=default_registry();started=time.monotonic()
    for n,strategy in enumerate(registry.selected(data['strategies'])):
        atomic_json(progress,{'strategy':strategy.id,'completed':n,'total':len(data['strategies']),
            'elapsed_seconds':round(time.monotonic()-started,1)})
        values=strategy.evaluate(context)
        AnalyticsService._rank_candidates(values,strategy,context,unique,run)
        del values
    atomic_json(output,{'items':[v.model_dump(mode='json') for v in unique.values()],
        'rejections':context.diagnostics,'elapsed_seconds':round(time.monotonic()-started,1)})

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--progress',type=Path,required=True)
    a=p.parse_args()
    try:calculate(a.snapshot,a.output,a.progress)
    except Exception as error:
        atomic_json(a.progress,{'error':type(error).__name__,'message':str(error)[:300]})
        raise

if __name__=='__main__':main()
# EOF
