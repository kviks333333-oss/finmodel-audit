#!/usr/bin/env python3
"""Independent frozen impact/financial-header evaluation; no API or Excel writes.

Application modules never import this script or its gold fixtures.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
CASES=ROOT/'tests/impact_cases'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def near(a,b):return type(a)in(int,float)and type(b)in(int,float)and math.isfinite(a)and math.isfinite(b)and math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8)
def save(path,data):
    if path.exists():raise FileExistsError(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def refs(values):return {(x['sheet'],x['cell'])for x in values if isinstance(x,dict)and isinstance(x.get('sheet'),str)and isinstance(x.get('cell'),str)}


def historical_result(workbook,spec,report):
    findings=[f for f in report.get('findings',[])if f.get('code')=='pv_fcff_historical_period']
    expected=spec['expected_historical'];found=bool(findings)
    invalid=[{'sheet':s,'cell':a}for f in findings for s,a in refs(f.get('evidence',[]))if s not in workbook.sheets or a not in workbook.sheets[s].cells]
    return {'id':spec['id'],'expected_historical':expected,'detected_historical':found,
            'true_positive':expected and found,'false_negative':expected and not found,'false_positive':not expected and found,
            'invalid_sources':invalid,'raw_finding_count':len(findings),'passed':expected==found and not invalid}


def scenario_checks(workbook,scenario):
    """Verify numeric envelopes and exact saved source values, without an oracle detector."""
    failures=[];baseline=scenario.get('baseline');result=scenario.get('result');delta=scenario.get('delta')
    numeric=type(baseline)in(int,float)and type(result)in(int,float)
    if not numeric or not near(delta,result-baseline):failures.append('delta_mismatch_or_nonfinite')
    relative=scenario.get('relative_delta')
    if baseline==0:
        if relative is not None:failures.append('zero_baseline_relative_must_be_null')
    elif type(baseline)in(int,float)and type(result)in(int,float):
        if not near(relative,(result-baseline)/abs(baseline)):failures.append('relative_delta_mismatch')
    proof={(x.get('sheet'),x.get('cell')):x for x in scenario.get('cell_evidence',[])}
    ignored=refs(scenario.get('ignored_sum_inputs',[]))
    for key,item in proof.items():
        sheet=workbook.sheets.get(key[0]);cell=sheet.cells.get(key[1])if sheet else None
        if item.get('present')is False:
            if cell is not None or item.get('value')is not None:failures.append('false_missing_cell_record')
        elif cell is None or cell.value!=item.get('value')or cell.formula!=item.get('formula'):
            failures.append('cell_evidence_mismatch')
    for item in scenario.get('source_inputs',[]):
        sheet=workbook.sheets.get(item.get('sheet'));cell=sheet.cells.get(item.get('cell'))if sheet else None
        source_proof=proof.get((item.get('sheet'),item.get('cell')),item)
        if cell is None or cell.value!=item.get('value')or isinstance(cell.value,bool)!=isinstance(item.get('value'),bool)or cell.formula!=source_proof.get('formula'):failures.append('source_input_mismatch')
        if (item.get('sheet'),item.get('cell'))in ignored:failures.append('ignored_input_presented_as_numeric_source')
    for sheet,address in refs(scenario.get('evidence',[])):
        if sheet not in workbook.sheets or address not in workbook.sheets[sheet].cells:
            item=proof.get((sheet,address),{})
            if (sheet,address)not in ignored or item.get('present')is not False:failures.append('missing_evidence_source')
    for patch in scenario.get('patches',[]):
        sheet=workbook.sheets.get(patch.get('sheet'));cell=sheet.cells.get(patch.get('cell'))if sheet else None
        if cell is None or cell.formula!=patch.get('original_formula'):failures.append('patch_original_formula_mismatch')
        if not isinstance(patch.get('changed_formula'),str)or not patch['changed_formula'].startswith('='):failures.append('changed_formula_missing')
    if scenario.get('conditional')is not True:failures.append('conditional_boundary_missing')
    if scenario.get('recalculated_workbook')is not False:failures.append('recalculation_boundary_missing')
    if not scenario.get('assumption'):failures.append('assumption_missing')
    return sorted(set(failures))


def impact_result(workbook,spec,expected,report):
    scenarios=report.get('scenarios',[]);checks=[{'id':s.get('id'),'failures':scenario_checks(workbook,s)}for s in scenarios]
    matched=[]
    kinds={'drop_duplicate_pv':'remove_sum_terms','drop_historical_pv':'remove_historical_sum_term','gordon_single_discount':'replace_gordon_numerator',
           'exclude_net_profit':'remove_net_profit','exclude_nopat':'remove_nopat'}
    for want in expected:
        matches=[s for s in scenarios if s.get('kind')==kinds[want['kind']]and s.get('code')==want['code']and s.get('target')==want['target']
                 and near(s.get('baseline'),want['baseline'])and near(s.get('result'),want['result'])]
        if want.get('changed_cell'):
            matches=[s for s in matches if len(s.get('patches',[]))==1 and s['patches'][0].get('cell')==want['changed_cell']
                     and want.get('removed_cell')in {r.get('cell')for r in s['patches'][0].get('excluded',[])}]
        matched.append({'kind':want['kind'],'scope':want['scope'],'changed_cell':want.get('changed_cell'),
            'baseline':want['baseline'],'result':want['result'],'matched_ids':[s.get('id')for s in matches],
            'required':want.get('required',True),'matched':bool(matches)})
    mode=spec['expectation'];unsafe=bool(scenarios)and mode in {'fail_closed','no_correction'}
    required=[x for x in matched if x['required']]
    return {'id':spec['id'],'expectation':mode,'scenario_count':len(scenarios),'expected_scenarios':matched,
            'numeric_source_checks':checks,'unsafe_scenario':unsafe,
            'required_matches':sum(x['matched']for x in required),'required_count':len(required),
            'explicit_limit':bool(report.get('checks') or report.get('limitations')),
            'coverage_complete':all(x['matched']for x in required)if mode=='required'else None,
            'no_source_or_boundary_failures':not unsafe and all(not x['failures']for x in checks)}


def nested_refs(value):
    result=refs([value])if isinstance(value,dict)else set()
    children=value.values()if isinstance(value,dict)else value if isinstance(value,list)else ()
    for child in children:
        if isinstance(child,(dict,list)):result|=nested_refs(child)
    return result


def wire_data(request):
    """Sources and scenario bodies from one actual serialized model request."""
    rows={};scenarios={}
    def walk(value):
        if isinstance(value,dict):
            for sheet,items in value.get('cell_data',{}).items():
                for row in items:
                    if isinstance(row,list)and len(row)==4:rows[sheet,row[0]]=row
            for scenario in value.get('impact_scenarios',[]):
                if isinstance(scenario,dict)and isinstance(scenario.get('id'),str):scenarios[scenario['id']]=scenario
            for child in value.values():
                if isinstance(child,(dict,list)):walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    for message in request.get('messages',[]):
        if message.get('role')not in {'user','function'}:continue
        try:body=json.loads(message.get('content',''))
        except(TypeError,ValueError):continue
        if isinstance(body,dict)and 'untrusted_workbook_data'in body:walk(body['untrusted_workbook_data'])
    return rows,scenarios


def delivered_impacts(transcript):
    turns=transcript.get('turns',[]);entries=transcript.get('tool_results',[]);results=[]
    for index,turn in enumerate(turns):
        call=turn.get('response',{}).get('choices',[{}])[0].get('message',{}).get('function_call',{})
        if call.get('name')!='calculate_impact'or turn.get('event')=='automatic_tool'or turn.get('replayed'):continue
        raw=turn.get('tool_result_raw',{}).get('impact_scenarios',[])
        entry=next((e for e in entries if e.get('tool')=='calculate_impact'and not e.get('replayed')and not e.get('automatic')
                    and e.get('arguments')==call.get('arguments')and e.get('impact_scenarios_raw')==raw),None)
        packed=entry.get('result',{})if entry else {}
        prepared_rows={(s,row[0]):row for s,items in packed.get('cell_data',{}).items()for row in items if isinstance(row,list)and len(row)==4}
        for original in raw:
            identity=original.get('id');body=next((s for s in packed.get('metadata',{}).get('impact_scenarios',[])if s.get('id')==identity),None)
            needed=nested_refs(body);delivery=[]
            clipped=bool(packed.get('cell_text_limits'))
            for next_index in range(index+1,len(turns)):
                request=turns[next_index].get('request')
                if not request:continue
                rows,scenarios=wire_data(request)
                if body is not None and scenarios.get(identity)==body and needed and not clipped and all(
                    node in prepared_rows and rows.get(node)==prepared_rows[node]for node in needed):delivery.append(next_index)
            results.append({'id':identity,'call_turn':index,'arguments':call.get('arguments'),'raw_scenario':original,
                'required_source_count':len(needed),'full_body_and_sources_in_same_request':bool(delivery),
                'delivered_source_records':[{'sheet':s,'cell':a,'value':prepared_rows[s,a][1],'formula':prepared_rows[s,a][2],'present':prepared_rows[s,a][3]!='blank'}for s,a in sorted(needed)]if delivery else [],
                'delivered_request_turns':delivery,'model_requested':True,'automatic':False,'replayed':False})
    return results


def live_evaluation(workbook,report,engine,gold,transcript_dir):
    public=lambda scenario:{k:v for k,v in scenario.items()if k!='cell_evidence'}
    canonical={s['id']:s for s in engine.get('scenarios',[])}
    top={s['id']:s for s in report.get('impact_analysis',{}).get('scenarios',[])}
    audit=next((s for s in report.get('stages',[])if s.get('id')=='audit'),{})
    attached={s['id']:s for s in audit.get('impact_calculations',[])}
    observations=[o for o in audit.get('observations',[])if isinstance(o.get('conditional_impact'),dict)]
    calls=[];envelopes=[]
    for path in sorted(transcript_dir.glob('*/*-transcript.json')):
        transcript=json.loads(path.read_text())
        if path.name=='audit-transcript.json':calls=delivered_impacts(transcript)
        for index,turn in enumerate(transcript.get('turns',[])):
            request=turn.get('request')
            if request:
                size=len(json.dumps({k:request[k]for k in ('messages','functions')},ensure_ascii=False,allow_nan=False,separators=(',',':')))
                envelopes.append({'stage':path.name.removesuffix('-transcript.json'),'turn':index,'characters':size,'within_45000':size<=45000})
    issues=[]
    if top!=canonical:issues.append('top_level_scenarios_differ_from_engine')
    for identity,scenario in attached.items():
        if public(canonical.get(identity,{}))!=public(scenario):issues.append('audit_scenario_differs_from_engine:'+identity)
        delivered=[c for c in calls if c['id']==identity and c['raw_scenario']==scenario and c['full_body_and_sources_in_same_request']]
        if not delivered:issues.append('audit_scenario_not_deliberately_delivered:'+identity)
        else:
            records={(x['sheet'],x['cell']):x for x in delivered[0]['delivered_source_records']}
            for original in canonical.get(identity,{}).get('cell_evidence',[]):
                row=records.get((original['sheet'],original['cell']),{})
                if row.get('value')!=original.get('value')or row.get('formula')!=original.get('formula')or row.get('present')!=(original.get('present')is not False):
                    issues.append('delivered_proof_differs_from_canonical_source:'+identity);break
        matches=[o for o in observations if o['conditional_impact'].get('id')==identity]
        if len(matches)!=1 or matches[0]['conditional_impact']!=scenario:issues.append('conditional_observation_proof_differs:'+identity)
        elif matches[0].get('origin')!='agent_requested_deterministic_scenario'or matches[0].get('status')!='not_verified':issues.append('conditional_observation_origin_or_status_wrong:'+identity)
    if {o['conditional_impact'].get('id')for o in observations}!=set(attached):issues.append('conditional_observation_without_matching_attachment')
    for identity,scenario in top.items():issues.extend(identity+':'+v for v in scenario_checks(workbook,scenario))
    unit=json.loads((CASES/'unit-evidence.json').read_text())['books'][gold['id']]
    if any(s.get('raw_unit')!=unit['raw_value']for s in top.values()):issues.append('raw_unit_differs_from_source')
    # Conditional observations are checked above, including genuine absent SUM
    # entries. The pre-existing base evaluator has no conditional-origin grammar.
    core=deepcopy(report)
    for stage in core.get('stages',[]):stage['observations']=[o for o in stage.get('observations',[])if 'conditional_impact'not in o]
    module_spec=importlib.util.spec_from_file_location('base_quality_evaluator',ROOT/'scripts/evaluate_agent_quality.py')
    quality=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(quality)
    core_gold=next(g for g in json.loads((ROOT/'tests/quality_cases/originals.json').read_text())['books']if g['id']==gold['id'])
    base=quality.agent_evaluation(workbook,core,core_gold,transcript_dir)
    business=next((s for s in report.get('stages',[])if s.get('id')=='business'),{})
    selected=set(business.get('selection',{}).get('fact_ids',[]));available=[c for c in report.get('facts',[])if c.get('stage_id')=='business']
    return {'issues':issues,'canonical_scenario_count':len(canonical),'top_level_scenario_count':len(top),'audit_impact_count':len(attached),
            'all_canonical_scenarios_requested':set(canonical)<={c['id']for c in calls},
            'all_canonical_scenarios_delivered_and_published':set(canonical)==set(attached),'conditional_observation_count':len(observations),
            'deliberate_calls':[{k:v for k,v in c.items()if k not in {'raw_scenario','delivered_source_records'}}for c in calls],
            'envelopes':envelopes,'max_envelope_characters':max((x['characters']for x in envelopes),default=0),'all_envelopes_within_45000':all(x['within_45000']for x in envelopes),
            'base_agent_evaluation':base,'business_selection':{'selected_count':len(selected),'available_count':len(available),
                'all_available_selected':selected=={c['id']for c in available},'selected_cards':[c for c in available if c['id']in selected],
                'comparison_count':len(business.get('selection',{}).get('comparisons',[]))},
            'scope':'Conditional observations checked separately from base fact/methodology evaluation; code-generated scenarios are not independent LLM discoveries.'}


def run(args):
    runtime=Path(args.runtime_dir).resolve();sys.path.insert(0,str(runtime));sys.path.insert(0,str(ROOT/'tests'))
    from finmodel_audit.financial import run_financial_checks
    from finmodel_audit.workbook import load_workbook
    from impact_cases.builders import historical_case,impact_case
    catalog=json.loads((CASES/'catalog.json').read_text());frozen=json.loads((CASES/'frozen.json').read_text())
    if not all(sha(ROOT/p)==h for p,h in frozen['files'].items()):raise ValueError('Frozen impact gold differs')
    destination=Path(args.output_dir).resolve()
    if (destination/'evaluation.json').exists():raise FileExistsError(destination/'evaluation.json')
    manifest={'evaluator_sha256':sha(Path(__file__)),'runtime':str(runtime),'runtime_sha256':{str(p.relative_to(runtime)):sha(p)for p in sorted((runtime/'finmodel_audit').rglob('*.py'))},
              'frozen_gold_sha256':sha(CASES/'frozen.json'),'api_calls':0,'source_writes':0}
    headers=[]
    for spec in catalog['historical_blind_cases']:
        wb=historical_case(spec);report=run_financial_checks(wb);result=historical_result(wb,spec,report);headers.append(result)
        save(destination/'headers'/f"{spec['id']}.json",{'result':result,'detector':report,'spec':spec})
    impacts=[];originals=[]
    live_paths={}
    for value in getattr(args,'live_report',[]):
        identity,separator,path=value.partition('=')
        if not separator or identity in live_paths or identity not in {'original_corporate','second_project'}:raise ValueError('--live-report requires unique known ID=PATH')
        live_paths[identity]=Path(path).resolve()
    if not args.headers_only:
        from finmodel_audit.impact import build_impact_analysis
        for spec in catalog['impact_cases']:
            wb,expected=impact_case(spec);report=build_impact_analysis(wb);result=impact_result(wb,spec,expected,report);impacts.append(result)
            save(destination/'impact'/f"{spec['id']}.json",{'result':result,'report':report})
        if not args.no_originals:
            for gold in json.loads((CASES/'originals.json').read_text())['books']:
                path=ROOT/gold['source']
                if not path.exists():originals.append({'id':gold['id'],'status':'source_missing'});continue
                before=sha(path)
                if before!=gold['sha256']:raise ValueError('Original SHA differs')
                wb=load_workbook(path);report=build_impact_analysis(wb);checks=[{'id':s.get('id'),'failures':scenario_checks(wb,s)}for s in report.get('scenarios',[])]
                wanted=[{'name':name,**quantity,'matching_ids':[s.get('id')for s in report.get('scenarios',[])if near(s.get('baseline'),gold['baseline'])and near(s.get('result'),quantity['result'])]}for name,quantity in gold['scenarios'].items()]
                record={'id':gold['id'],'status':'evaluated','source_sha256':before,'source_unchanged':sha(path)==before,'frozen_conditional_quantities':wanted,'scenario_checks':checks,
                        'scenario_count':len(report.get('scenarios',[])),'note':'Combined/all-period gold variants are shown separately; missing coverage does not imply one-period scenarios are numerically wrong.'}
                if gold['id']in live_paths:
                    live_path=live_paths[gold['id']];report_hash=sha(live_path)
                    record['live']=live_evaluation(wb,json.loads(live_path.read_text()),report,gold,live_path.parent/'checkpoints')
                    record['live_report_sha256']=report_hash;record['live_report_unchanged']=sha(live_path)==report_hash
                originals.append(record);save(destination/'originals'/f"{gold['id']}.json",{'result':record,'report':report})
    summary={'historical_blind':{'cases':len(headers),'positive_cases':sum(x['expected_historical']for x in headers),'true_positives':sum(x['true_positive']for x in headers),'false_negatives':sum(x['false_negative']for x in headers),'false_positives':sum(x['false_positive']for x in headers),'invalid_source_findings':sum(bool(x['invalid_sources'])for x in headers)},
             'impact':{'cases':len(impacts),'unsafe_cases':sum(x['unsafe_scenario']for x in impacts),'numeric_source_boundary_failures':sum(not x['no_source_or_boundary_failures']for x in impacts),'required_complete_cases':sum(x['coverage_complete']is True for x in impacts),'required_cases':sum(x['expectation']=='required'for x in impacts)}}
    output={'manifest':manifest,'summary':summary,'historical_blind':headers,'impact_cases':impacts,'originals':originals,
            'limitations':['Header controls are fresh variants after the previous held-out set was exposed; this is a small targeted set, not broad generalization.','Conditional result matching is numerical evidence, not economic validation of a correction choice.','Individual scenario effects must not be added without an explicit joint evaluation.','No Excel recalc, macro, external refresh or model API occurred.']}
    save(destination/'evaluation.json',output);print(json.dumps(summary,ensure_ascii=False));return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True);parser.add_argument('--runtime-dir',default=str(ROOT/'src'))
    parser.add_argument('--headers-only',action='store_true');parser.add_argument('--no-originals',action='store_true')
    parser.add_argument('--live-report',action='append',default=[],metavar='ID=PATH')
    run(parser.parse_args())
