import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import torch
from autodop.engine import PolicyEngine


class Model:
    def __init__(self):self.calls=0
    def eval(self):return self
    def __call__(self, graph):
        self.calls+=1
        return torch.tensor([[0.,10.]])


class PolicyDecisionCacheTest(unittest.TestCase):
    def setUp(self):
        self.model=Model()
        self.loader=patch('autodop.engine.load_policy_model',return_value=(self.model,{'metadata':{'dop_classes':[1,4]}}))
        self.graph=patch('autodop.engine.build_graph',return_value={})
        self.loader.start();self.graph.start()
        self.addCleanup(self.loader.stop);self.addCleanup(self.graph.stop)
        self.request={'query_id':'first','fragments':[{'fragment_id':'0','fragment_index':0,
            'worker_ids':[1],'candidate_dops':[1,4]}],'edges':[],
            'worker_loads':{'1':{'fresh':True,'cpu_used':0.,'mem_used_pct':0.}}}

    def test_live_load_and_query_identity_are_not_replayed(self):
        engine=PolicyEngine('unused',{'cpu_threshold':.4,'mem_threshold':.65,'floor':.1},cache_capacity=2)
        first=engine.decide(self.request)
        high=copy.deepcopy(self.request);high['query_id']='second';high['worker_loads']['1']['cpu_used']=1.
        changed=engine.decide(high)
        self.assertEqual(self.model.calls,1)
        self.assertEqual(first['decisions'][0]['dop'],4)
        self.assertEqual(changed['decisions'][0]['dop'],1)
        self.assertEqual(changed['query_id'],'second')
        self.assertEqual(engine.decide(self.request)['decisions'][0]['dop'],4)

    def test_mutated_response_does_not_poison_cache(self):
        engine=PolicyEngine('unused',cache_capacity=1)
        engine.decide(self.request)['decisions'][0]['dop']=99
        self.assertEqual(engine.decide(self.request)['decisions'][0]['dop'],4)
        self.assertEqual(self.model.calls,1)

    def test_placement_domains_and_graph_changes_invalidate(self):
        engine=PolicyEngine('unused',cache_capacity=4)
        engine.decide(self.request)
        for field,value in [('worker_ids',[2]),('candidate_dops',[1,2,4]),('estimated_cardinality',1000)]:
            other=copy.deepcopy(self.request);other['fragments'][0][field]=value
            engine.decide(other)
        self.assertEqual(self.model.calls,4)

    def test_bounded_lru_clear_and_disabled_default(self):
        engine=PolicyEngine('unused',cache_capacity=1)
        engine.decide(self.request)
        other=copy.deepcopy(self.request);other['fragments'][0]['worker_ids']=[2]
        engine.decide(other);engine.decide(self.request)
        self.assertEqual(self.model.calls,3)
        self.assertEqual(len(engine._decision_cache),1)
        engine.clear_decision_cache();engine.decide(self.request)
        self.assertEqual(self.model.calls,4)
        default=PolicyEngine('unused');default.decide(self.request);default.decide(self.request)
        self.assertEqual(self.model.calls,6)

    def test_concurrent_hits_return_independent_responses(self):
        engine=PolicyEngine('unused',cache_capacity=2);engine.decide(self.request)
        def work(i):
            request=copy.deepcopy(self.request);request['query_id']=str(i)
            result=engine.decide(request);result['decisions'][0]['dop']=i
            return result['query_id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(work,range(20))),list(map(str,range(20))))
        self.assertEqual(self.model.calls,1)
        self.assertEqual(engine.decide(self.request)['decisions'][0]['dop'],4)

    def test_invalid_capacity(self):
        for value in [-1,1.2,True]:
            with self.assertRaises(ValueError):PolicyEngine('unused',cache_capacity=value)


if __name__=='__main__':unittest.main()
