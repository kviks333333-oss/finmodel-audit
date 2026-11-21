"""Independent impact controls, not implementation-mirroring detector tests."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from impact_cases.builders import impact_case,historical_case

ROOT=Path(__file__).resolve().parents[1]
module=importlib.util.spec_from_file_location('impact_evaluator',ROOT/'scripts/evaluate_impact.py')
evaluation=importlib.util.module_from_spec(module);module.loader.exec_module(evaluation)
CATALOG=json.loads((ROOT/'tests/impact_cases/catalog.json').read_text())


class ImpactEvaluationTests(unittest.TestCase):
    def case(self,variant):return next(x for x in CATALOG['impact_cases']if x['variant']==variant)

    def test_frozen_gold_has_not_changed_after_implementation(self):
        manifest=json.loads((ROOT/'tests/impact_cases/frozen.json').read_text())
        for path,digest in manifest['files'].items():self.assertEqual(evaluation.sha(ROOT/path),digest,path)

    def test_original_ev_conditional_quantities_are_independent_arithmetic(self):
        gold=json.loads((ROOT/'tests/impact_cases/originals.json').read_text())['books'][0]
        c={x['cell']:x['value']for x in gold['source_cells']};s=gold['scenarios']
        self.assertAlmostEqual(s['drop_duplicate_pv']['result'],c['E55']-c['P39'])
        self.assertAlmostEqual(s['drop_historical_pv']['result'],c['E55']-c['H39'])
        value=c['E39']-c['P39']-c['H39']+c['P27']*(1+c['E14'])/(c['E13']-c['E14'])*c['N37']
        self.assertAlmostEqual(s['all_three_conditional']['result'],value)
        self.assertNotEqual(s['gordon_single_discount']['result'],s['all_three_conditional']['result'])

    def test_original_project_alternatives_are_distinct_not_one_declared_fix(self):
        gold=json.loads((ROOT/'tests/impact_cases/originals.json').read_text())['books'][1]
        c={x['cell']:x['value']for x in gold['source_cells']}
        np=gold['baseline']-sum(c[col+'19']*c[col+'28']for col in 'DEFGH')
        nopat=gold['baseline']-sum(c[col+'20']*c[col+'28']for col in 'DEFGH')
        self.assertAlmostEqual(np,gold['scenarios']['exclude_net_profit_from_fcff']['result'])
        self.assertAlmostEqual(nopat,gold['scenarios']['exclude_nopat_from_fcff']['result'])
        self.assertNotEqual(np,nopat)

    def test_zero_output_has_null_relative_delta_and_negative_removal_increases_output(self):
        _,gold=impact_case(self.case('duplicate_zero'));g=gold[0]
        self.assertEqual(g['baseline'],0);self.assertIsNone(g['relative_delta']);self.assertGreater(g['result'],0)

    def test_negative_baseline_relative_uses_absolute_denominator(self):
        _,gold=impact_case(self.case('project_negative'))
        for g in gold:
            self.assertLess(g['baseline'],0)
            self.assertAlmostEqual(g['relative_delta'],g['delta']/abs(g['baseline']))

    def test_scaling_and_cross_sheet_target_preserve_impact_not_source_units(self):
        _,plain=impact_case(self.case('duplicate_plain'));_,scaled=impact_case(self.case('duplicate_scaled'))
        self.assertAlmostEqual(scaled[0]['result'],plain[0]['result']*1000)
        wb,cross=impact_case(self.case('duplicate_cross_sheet'))
        self.assertNotEqual(cross[0]['target']['sheet'],next(iter(wb.sheets)))
        self.assertEqual(cross[0]['result'],plain[0]['result'])

    def test_nonadditivity_requires_joint_expression_not_sum_of_effects(self):
        base,a,b=20,3,5
        joint=(base-a-b)**2-base**2
        additive=((base-a)**2-base**2)+((base-b)**2-base**2)
        self.assertNotEqual(joint,additive)
        _,linear=impact_case(self.case('duplicate_plain'));_,nonlinear=impact_case(self.case('duplicate_nonlinear'))
        self.assertAlmostEqual(nonlinear[0]['delta'],linear[0]['result']**2-linear[0]['baseline']**2)

    def test_scenario_boundaries_and_source_values_are_required(self):
        wb,gold=impact_case(self.case('duplicate_plain'));g=gold[0];sheet=next(iter(wb.sheets));cell=wb.sheets[sheet].cells['B4']
        scenario={**g,'conditional':True,'recalculated_workbook':False,'assumption':'Remove only the extra sum occurrence',
                  'source_inputs':[{'sheet':sheet,'cell':'B4','value':cell.value,'formula':cell.formula}]}
        self.assertEqual(evaluation.scenario_checks(wb,scenario),[])
        bad=deepcopy(scenario);bad['source_inputs'][0]['value']=99;bad['conditional']=False;bad['relative_delta']*=-1
        self.assertEqual(set(evaluation.scenario_checks(wb,bad)),{'source_input_mismatch','conditional_boundary_missing','relative_delta_mismatch'})

    def test_unsupported_dynamic_source_must_not_publish_numeric_scenario(self):
        spec=self.case('duplicate_dynamic');wb,gold=impact_case(spec)
        result=evaluation.impact_result(wb,spec,gold,{'scenarios':[{'id':'unsafe'}]})
        self.assertTrue(result['unsafe_scenario']);self.assertFalse(result['no_source_or_boundary_failures'])

    def test_input_formula_can_live_in_full_proof_and_missing_sum_cells_are_not_zero_inputs(self):
        wb,gold=impact_case(self.case('duplicate_plain'));g=gold[0];sheet=next(iter(wb.sheets));cell=wb.sheets[sheet].cells['F11']
        missing={'sheet':sheet,'cell':'Z99'}
        scenario={**g,'conditional':True,'recalculated_workbook':False,'assumption':'Scoped counterfactual',
          'source_inputs':[{'sheet':sheet,'cell':'F11','value':cell.value}],
          'cell_evidence':[{'sheet':sheet,'cell':'F11','value':cell.value,'formula':cell.formula},{**missing,'present':False,'value':None}],
          'evidence':[missing],'ignored_sum_inputs':[missing]}
        self.assertEqual(evaluation.scenario_checks(wb,scenario),[])
        scenario['source_inputs'].append({**missing,'value':0})
        self.assertIn('ignored_input_presented_as_numeric_source',evaluation.scenario_checks(wb,scenario))

    def test_partial_period_effect_does_not_match_all_period_gold(self):
        spec=self.case('project_plain');wb,gold=impact_case(spec);one=next(x for x in gold if x['scope']=='one_overlap_formula')
        result=evaluation.impact_result(wb,spec,gold,{'scenarios':[{'id':'one',**one}]})
        combined=[x for x in result['expected_scenarios']if x['scope']=='all_identified_overlap_terms']
        self.assertTrue(all(not x['matched']for x in combined))

    def test_fresh_blind_cases_include_clean_ambiguity_and_real_calendar_cases(self):
        cases=CATALOG['historical_blind_cases'];self.assertEqual((len(cases),sum(x['expected_historical']for x in cases)),(12,5))
        for spec in cases:
            wb=historical_case(spec);self.assertIn(spec['sheet'],wb.sheets)
            self.assertTrue(all(c.address==a for s in wb.sheets.values()for a,c in s.cells.items()))

    def test_artifacts_are_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory()as directory:
            path=Path(directory)/'result.json';evaluation.save(path,{'original':True})
            with self.assertRaises(FileExistsError):evaluation.save(path,{'original':False})

    def test_live_scenario_requires_body_and_all_sources_in_one_later_request(self):
        scenario={'id':'impact_x','target':{'sheet':'S','cell':'A1'},'source_inputs':[{'sheet':'S','cell':'B1','value':10}]}
        arguments={'scenario_id':'impact_x'};rows={'S':[['A1',20,'=B1*2','formula_number'],['B1',10,None,'number']]}
        packed={'metadata':{'impact_scenarios':[scenario]},'cell_data':rows}
        entry={'tool':'calculate_impact','arguments':arguments,'impact_scenarios_raw':[scenario],'result':packed}
        turn={'response':{'choices':[{'message':{'function_call':{'name':'calculate_impact','arguments':arguments}}}]},'tool_result_raw':{'impact_scenarios':[scenario]}}
        def request(data):return {'request':{'messages':[{'role':'function','content':json.dumps({'untrusted_workbook_data':data})}]}}
        complete={'turns':[turn,request(packed)],'tool_results':[entry]}
        self.assertTrue(evaluation.delivered_impacts(complete)[0]['full_body_and_sources_in_same_request'])
        no_body={'turns':[turn,request({'cell_data':rows})],'tool_results':[entry]}
        self.assertFalse(evaluation.delivered_impacts(no_body)[0]['full_body_and_sources_in_same_request'])
        split={'turns':[turn,request({'metadata':packed['metadata'],'cell_data':{'S':rows['S'][:1]}}),request({'cell_data':{'S':rows['S'][1:]}})],'tool_results':[entry]}
        self.assertFalse(evaluation.delivered_impacts(split)[0]['full_body_and_sources_in_same_request'])

    def test_replayed_or_automatic_impact_is_not_agent_requested_evidence(self):
        for flags in ({'replayed':True},{'event':'automatic_tool'}):
            transcript={'turns':[{'response':{'choices':[{'message':{'function_call':{'name':'calculate_impact'}}}]},**flags}]}
            self.assertEqual(evaluation.delivered_impacts(transcript),[])


if __name__=='__main__':unittest.main()
