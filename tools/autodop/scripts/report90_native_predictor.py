"""Native CSV predictor for a bounded V2 admission experiment, not production."""
import csv
import io
import json
from pathlib import Path
import numpy as np


class NativePredictor:
    def __init__(self, model):
        self.model=model
        self.header=model['csv_header']
        self.indices=[self.header.index(k) for k in model['feature_names']]
        assert all(k.startswith(('env_','operators_')) for k in model['feature_names'])
        self.mean=np.array(model['mean'],dtype=float)
        self.scale=np.array(model['scale'],dtype=float)
        self.training=np.array(model['standardized_training_features'],dtype=float)
        self.targets=np.array(model['training_peak_bytes'],dtype=np.int64)
        assert self.training.shape==(len(self.targets),len(self.indices)) and len(self.targets)>=3
        assert self.mean.shape==self.scale.shape==(len(self.indices),)
        assert np.isfinite(self.training).all() and np.isfinite(self.mean).all()
        assert np.isfinite(self.scale).all() and (self.scale>0).all() and (self.targets>0).all()
        self.slot_bytes=int(model['slot_bytes'])
        self.slots_per_worker=int(model['slots_per_worker'])
        self.workers=int(model['workers'])
        assert self.slot_bytes>0 and self.slots_per_worker==32 and self.workers==3

    @classmethod
    def load(cls,path):
        return cls(json.loads(Path(path).read_text()))

    def encode(self,budget):
        if type(budget) is not int or budget<=0:raise ValueError('invalid budget bytes')
        needed=(budget*5+3*self.slot_bytes-1)//(3*self.slot_bytes)
        rounded=1<<(max(needed,1)-1).bit_length()
        granted=min(rounded,self.slots_per_worker)
        return dict(encoded_cost_bytes=granted*self.slot_bytes,
                    expected_physical_slots=granted*self.workers,
                    nominal_bytes_per_worker=granted*self.slot_bytes,
                    saturated=rounded>self.slots_per_worker,
                    covers_scaled_budget=granted*self.slot_bytes*3>=budget*5)

    def predict_csv(self,body,mode='predicted'):
        rows=list(csv.reader(io.StringIO(body)))
        if len(rows)!=2 or rows[0]!=self.header or len(rows[1])!=len(self.header):
            raise ValueError('native CSV schema mismatch')
        try:values=[int(v) for v in rows[1]]
        except ValueError as error:raise ValueError('native CSV requires integer features') from error
        for name,expected in self.model['expected_environment'].items():
            if values[self.header.index(name)]!=expected:raise ValueError('BE environment changed: '+name)
        if mode=='fixed8':
            # 4 slots/BE x3BE =12; total96 /12 gives8 simultaneous normal queries.
            return dict(mode=mode,encoded_cost_bytes=4*self.slot_bytes,expected_physical_slots=12,
                        nominal_bytes_per_worker=4*self.slot_bytes,saturated=False)
        if mode!='predicted':raise ValueError('unsupported predictor mode')
        x=np.array([values[i] for i in self.indices],dtype=float)
        if not np.isfinite(x).all():raise ValueError('nonfinite features')
        distances=np.linalg.norm(self.training-(x-self.mean)/self.scale,axis=1)
        nearest=np.argsort(distances,kind='stable')[:3]
        predicted=int(max(self.targets[nearest]))
        budget=max(64*1024**2,(5*predicted+3)//4)
        return dict(mode=mode,predicted_peak_bytes=predicted,budget_bytes=budget,
                    neighbor_indices=nearest.tolist(),**self.encode(budget))
