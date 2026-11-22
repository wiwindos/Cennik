"""Publication provenance for automatic and explicitly requested analytics."""
import hashlib
import json
from uuid import NAMESPACE_URL,uuid5

HEAVY=frozenset({'craft_graph','craft_disenchant'})

def config_key(config):
    return hashlib.sha256(json.dumps(config.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()

def extended_stale(opportunity,generation,config):
    evidence=opportunity.details.get('extended_analysis')
    return bool(opportunity.details.get('extended_stale') or evidence and (evidence['generation']!=generation or evidence['config']!=config_key(config)))

def require_current(opportunity,generation,config):
    if extended_stale(opportunity,generation,config):
        raise ValueError('Расширенная аналитика устарела. Нажмите «Рассчитать цепочки и распыление», затем повторите выбор.')

def rehome(opportunity,run):
    return opportunity.model_copy(update={'id':uuid5(NAMESPACE_URL,run.run_id+'/'+opportunity.id).hex,
        'run_id':run.run_id,'data_age_seconds':max(0,int((run.as_of-opportunity.observed_at).total_seconds()))})

# EOF
