"""The external quality harness must score evidence honestly and preserve inputs."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from finmodel_audit.workbook import load_workbook
from quality_cases.builders import build_case

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('quality_evaluator',ROOT/'scripts/evaluate_agent_quality.py')
evaluator=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluator)
CATALOG=json.loads((ROOT/'tests/quality_cases/catalog.json').read_text())


class QualityEvaluationTests(unittest.TestCase):
    def case(self,identity):
        return next(c for c in CATALOG['cases'] if c['id']==identity)

    def test_fixed_disjoint_development_and_holdout_splits(self):
        development={c['id'] for c in CATALOG['cases'] if c['split']=='development'}
        heldout={c['id'] for c in CATALOG['cases'] if c['split']=='held_out'}
        self.assertEqual((len(development),len(heldout)),(12,8))
        self.assertFalse(development&heldout)
        self.assertTrue(all(c.get('row_shift',0)>0 and c.get('column_shift',0)>0 for c in CATALOG['cases'] if c['split']=='held_out'))

    def test_new_synthetic_xlsx_roundtrips_formulas_datatables_and_preserves_model(self):
        wb,gold=build_case(self.case('train_dcf_stale_grid'));before=deepcopy(wb)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.xlsx';evaluator.write_fixture(wb,path);loaded=load_workbook(path)
            for name,sheet in wb.sheets.items():
                for address,cell in sheet.cells.items():
                    actual=loaded.sheets[name].cells[address]
                    expected_attrs={**cell.formula_attributes,**({'t':cell.formula_type} if cell.formula_type in {'array','dataTable','shared'} else {})}
                    self.assertEqual((actual.value,actual.formula,actual.formula_type,actual.formula_attributes),
                                     (cell.value,cell.formula,cell.formula_type,expected_attrs))
            with self.assertRaises(FileExistsError):evaluator.write_fixture(wb,path)
        self.assertEqual(wb,before)

    def test_synthetic_archive_bytes_are_reproducible(self):
        wb,_=build_case(self.case('train_project_clean'))
        with tempfile.TemporaryDirectory() as directory:
            first=Path(directory)/'a.xlsx';second=Path(directory)/'b.xlsx'
            evaluator.write_fixture(wb,first);evaluator.write_fixture(wb,second)
            self.assertEqual(first.read_bytes(),second.read_bytes())

    def test_actual_sent_blank_is_explicit_absence_not_numeric_source(self):
        data={'cell_data':{'S':[['A1',None,None,'blank'],['B1',None,None,'boolean_or_empty']]}}
        shown,absent,_=evaluator.collect_payload(data)
        self.assertEqual(absent,{('S','A1')});self.assertEqual(shown,{('S','B1')})

    def test_moved_absolute_calendar_references_still_point_to_labeled_cells(self):
        wb,gold=build_case(self.case('holdout_H'))
        calendar=next(q for q in gold['questions'] if q['topic']=='calendar')
        missing=calendar['required_absent'][0]
        self.assertNotEqual(missing['cell'],'J14')
        sheet=wb.sheets[missing['sheet']]
        self.assertNotIn(missing['cell'],sheet.cells)
        self.assertTrue(any('EDATE' in (c.formula or '') and missing['cell'] in c.formula.replace('$','') for c in sheet.cells.values()))

    def test_explicit_zero_is_a_clean_input_not_a_missing_reference(self):
        clean,_=build_case(self.case('train_project_explicit_zero_riskfree'))
        bad,gold=build_case(self.case('train_project_missing_riskfree_broken_dates'))
        a=next(iter(clean.sheets.values())).cells['C3'];b=next(iter(bad.sheets.values())).cells['C3']
        self.assertEqual((a.value,b.value),(0,0));self.assertIsNone(a.formula);self.assertEqual(b.formula,'=J3')
        self.assertTrue(any(q['required_absent'] for q in gold['questions']))
        sheet=next(iter(clean.sheets.values()))
        for col in 'DEF':self.assertGreater(sheet.cells[col+'16'].value,sheet.cells[col+'15'].value)

    def test_detector_counts_themes_not_duplicate_findings(self):
        wb,gold=build_case(self.case('train_project_profit_overlap'))
        issue='fcff_net_profit_nopat_overlap'
        finding={'id':'a','code':issue,'evidence':gold['proof'][issue]}
        result=evaluator.detector_evaluation(wb,{'expected':[issue],**gold},{'findings':[finding]*5},CATALOG['monitored_codes'])
        self.assertEqual(result['true_positives'],[issue]);self.assertEqual(result['raw_finding_count'],5)
        clean=evaluator.detector_evaluation(wb,{'expected':[]},{'findings':[finding,{'id':'b','code':'new_unscored','evidence':[]}]},CATALOG['monitored_codes'])
        self.assertEqual(clean['false_positives'],[issue]);self.assertEqual(clean['unscored_codes'],['new_unscored'])

    def test_partial_proofs_are_not_combined_into_one_complete_finding(self):
        wb,gold=build_case(self.case('train_dcf_repeat_alias'));code='pv_fcff_duplicate_term';core=gold['proof'][code]
        findings=[{'code':code,'evidence':core[:3]},{'code':code,'evidence':core[3:]}]
        result=evaluator.detector_evaluation(wb,{'expected':[code],**gold},{'findings':findings},CATALOG['monitored_codes'])
        self.assertFalse(result['proof_core'][code]['complete_in_one_finding'])

    def test_prepared_or_automatic_data_is_not_direct_agent_examination(self):
        def request(cell):return {'messages':[{'role':'user','content':json.dumps({'untrusted_workbook_data':{'cell_data':{'S':[[cell,1,None,'number']]}}})}]}
        transcript={'turns':[{'event':'automatic_tool','tool_result_raw':{'sheet':'S','cell':'A1','value':1}},
                            {'request':request('A1'),'response':{'choices':[{'message':{'function_call':{'name':'read_cells','arguments':{}}}}]},
                             'tool_result_raw':{'sheet':'S','cell':'B1','value':2}}]}
        result=evaluator.request_evidence(transcript)
        self.assertEqual(result['shown'],{('S','A1')});self.assertEqual(result['direct'],set())
        transcript['turns'].append({'request':request('B1')})
        self.assertEqual(evaluator.request_evidence(transcript)['direct'],{('S','B1')})

    def test_source_only_seen_before_final_tool_result_is_not_delivery_of_that_result(self):
        transcript={'turns':[{'request':{'messages':[{'role':'function','content':json.dumps({'untrusted_workbook_data':{
            'cell_data':{'S':[['A1',1,None,'number']]}}})}]},
            'response':{'choices':[{'message':{'function_call':{'name':'read_cells','arguments':{}}}}]},
            'tool_result_raw':{'sheet':'S','cell':'A1','value':1}}]}
        result=evaluator.request_evidence(transcript)
        self.assertEqual(result['shown'],{('S','A1')});self.assertEqual(result['direct'],set())

    def test_inspection_sources_do_not_prove_omitted_question_metadata_was_delivered(self):
        ref={'sheet':'S','cell':'A1'}
        question={'id':'q','category':'rate','evidence':[ref],
                  'issues':[{'code':'example','evidence':[ref]}],
                  'components':[{'source':ref,'label_source':ref,'value':1}]}
        data={'cell_data':{'S':[['A1',1,None,'number']]}}
        def transcript(payload):
            return {'turns':[{'response':{'choices':[{'message':{'function_call':{'name':'inspect_question','arguments':{}}}}]},
                'tool_result_raw':{'investigation_question':question,'cell_evidence':[{**ref,'value':1}]}},
                {'request':{'messages':[{'role':'function','content':json.dumps({'untrusted_workbook_data':payload})}]}}]}
        result=evaluator.request_evidence(transcript(data))['inspections'][0]
        self.assertTrue(result['source_bundle_fully_sent'])
        self.assertFalse(result['question_metadata_delivered']);self.assertEqual(result['issues'],[]);self.assertEqual(result['components'],[])
        for addition in ({'metadata':{'investigation_question':question}},{'investigation_questions':[question]}):
            delivered=evaluator.request_evidence(transcript({**data,**addition}))['inspections'][0]
            self.assertTrue(delivered['question_metadata_delivered']);self.assertEqual(delivered['issues'],question['issues'])
        changed=deepcopy(question);changed['issues']=[]
        partial=evaluator.request_evidence(transcript({**data,'metadata':{'investigation_question':changed}}))['inspections'][0]
        self.assertFalse(partial['question_metadata_delivered'])

    def test_matching_label_does_not_hide_wrong_numeric_value(self):
        wb,gold=build_case(self.case('train_project_clean'));sheet=next(iter(wb.sheets));cell=wb.sheets[sheet].cells['C12']
        report={'facts':[{'stage_id':'assumptions','id':'same-label','source_sheet':sheet,'value_cell':'C12','value':999,
                         'formula':cell.formula,'metric_label':'WACC','metric_cell':'A12','unit':'%','unit_cell':'B12'}],
                'stages':[],'calculations':[]}
        with tempfile.TemporaryDirectory() as directory:
            result=evaluator.agent_evaluation(wb,report,{'proof':{},'expected':[],'questions':[]},Path(directory))
        self.assertEqual(result['card_value_formula_matches'],0)
        self.assertEqual(result['numeric_or_source_issues'][0]['type'],'fact_numeric_source_mismatch')

    def test_structured_proof_checks_saved_values_and_preserves_code_origin(self):
        wb,_=build_case(self.case('train_project_clean'));sheet=next(iter(wb.sheets));cell=wb.sheets[sheet].cells['C12']
        report={'stages':[{'id':'audit','observations':[{'evidence':[{'sheet':sheet,'cell':'C12'}],
            'methodology':{'origin':'deterministic_finding','source_cells':[{'sheet':sheet,'cell':'C12','value':999,'formula':cell.formula,'present':True}]}}]}]}
        with tempfile.TemporaryDirectory() as directory:
            result=evaluator.agent_evaluation(wb,report,{'proof':{},'expected':[]},Path(directory))
        proof=result['structured_methodology'][0]
        self.assertFalse(proof['source_records_match']);self.assertFalse(proof['independent_discovery_claimed'])
        self.assertEqual(result['numeric_or_source_issues'][0]['type'],'methodology_source_mismatch')

    def test_recorded_arithmetic_is_bounded_and_does_not_execute_python(self):
        self.assertEqual(evaluator.local_arithmetic('sum(a,b,c)+r**2',{'a':1,'b':2,'c':3,'r':2}),10)
        for expression in ("__import__('os').getcwd()",'2**100000',"open('x')",'[x for x in y]'):
            with self.assertRaises(ValueError):evaluator.local_arithmetic(expression,{})

    def test_evaluation_json_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json';evaluator.write_json(path,{'ok':True})
            with self.assertRaises(FileExistsError):evaluator.write_json(path,{'ok':False})
            self.assertEqual(json.loads(path.read_text()),{'ok':True})


if __name__=='__main__':unittest.main()
