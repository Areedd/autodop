import json
from pathlib import Path
import pytest
from report90_native_predictor import NativePredictor


def predictor():
    return NativePredictor(dict(csv_header=['tables_0','env_0','operators_0','var_0'],
          feature_names=['env_0','operators_0'],mean=[3,0],scale=[1,1],
          standardized_training_features=[[0,0],[0,1],[0,2],[0,100]],
          training_peak_bytes=[100,200,300,10000],slot_bytes=10000000,
          slots_per_worker=32,workers=3,expected_environment={'env_0':3}))


def test_table_ids_and_dop_do_not_select_neighbors():
    p=predictor()
    a=p.predict_csv('tables_0,env_0,operators_0,var_0\n999,3,1,4')
    b=p.predict_csv('tables_0,env_0,operators_0,var_0\n123,3,1,16')
    assert a==b and a['predicted_peak_bytes']==300
    assert a['nominal_bytes_per_worker']*3>=a['budget_bytes']*5


def test_rounding_boundaries_preserve_headroom_or_mark_saturation():
    p=predictor()
    for b in [1,5999999,6000000,6000001,9000000,11999999,12000000,12000001,192000000,192000001]:
        r=p.encode(b)
        assert r['expected_physical_slots']<=96
        if not r['saturated']:assert r['nominal_bytes_per_worker']*3>=b*5
        else:assert not r['covers_scaled_budget']


def test_fixed8_is_constant_and_unknown_schema_or_environment_rejected():
    p=predictor()
    a=p.predict_csv('tables_0,env_0,operators_0,var_0\n0,3,999,0','fixed8')
    assert a['expected_physical_slots']==12 and a['encoded_cost_bytes']==40000000
    for text in ['env_0,tables_0,operators_0,var_0\n3,0,1,4',
                 'tables_0,env_0,operators_0,var_0\n0,4,1,4',
                 'tables_0,env_0,operators_0,var_0\n0,3,NaN,4']:
        with pytest.raises(ValueError):p.predict_csv(text)
