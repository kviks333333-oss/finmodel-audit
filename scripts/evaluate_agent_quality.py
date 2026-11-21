#!/usr/bin/env python3
"""Independent offline quality evaluation. Never imported by the application.

Labels are test-only. This evaluates stored reports and deterministic code;
it never calls an LLM, reads credentials, refreshes links or recalculates Excel.
"""
from __future__ import annotations
import argparse
import ast
import operator
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
CASES=ROOT/'tests/quality_cases'
TOPICS={
 'pv_fcff_duplicate_term':r'дубли|повторн\w*\s+(?:включ|уч[её]т).*(?:поток|слага)|duplicate',
 'pv_fcff_historical_period':r'историч|заверш[её]нн.*период|границ.*(?:прогноз|ряд)|при\s+дате\s+оценки|historical',
 'gordon_double_discount':r'Gordon|GGM|(?:терминал|terminal).*(?:дисконт|discount)|(?:дисконт|discount).*(?:терминал|terminal)',
 'datatable_basepoint_cache_mismatch':r'таблиц|сетк|data.?table|базов.*точк',
 'irr_on_discounted_cashflows':r'\bIRR\b',
 'fcff_net_profit_nopat_overlap':r'NOPAT|двойн.*прибыл|повтор.*прибыл',
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def refs(items):
    return {(x['sheet'],x['cell']) for x in items if isinstance(x,dict) and isinstance(x.get('sheet'),str) and isinstance(x.get('cell'),str)}


def addresses(items):
    return [{'sheet':s,'cell':a} for s,a in sorted(items)]


def near(a,b):
    return type(a) in (int,float) and type(b) in (int,float) and math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8)


def write_json(path,data):
    if path.exists(): raise FileExistsError(f'Refusing to overwrite evaluation artifact: {path}')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def write_fixture(workbook,path):
    """Write a NEW minimal OOXML synthetic fixture, never an original/copy input."""
    if path.exists():raise FileExistsError(path)
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    wb=ET.Element('workbook',{'xmlns':ns,'xmlns:r':rel}); sheets=ET.SubElement(wb,'sheets')
    relationships=ET.Element('Relationships',{'xmlns':'http://schemas.openxmlformats.org/package/2006/relationships'})
    types=ET.Element('Types',{'xmlns':'http://schemas.openxmlformats.org/package/2006/content-types'})
    ET.SubElement(types,'Default',{'Extension':'rels','ContentType':'application/vnd.openxmlformats-package.relationships+xml'})
    ET.SubElement(types,'Default',{'Extension':'xml','ContentType':'application/xml'})
    ET.SubElement(types,'Override',{'PartName':'/xl/workbook.xml','ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'})
    parts={}
    for i,(name,sheet) in enumerate(workbook.sheets.items(),1):
        ET.SubElement(sheets,'sheet',{'name':name,'sheetId':str(i),'state':sheet.state,'r:id':f'rId{i}'})
        ET.SubElement(relationships,'Relationship',{'Id':f'rId{i}','Type':rel+'/worksheet','Target':f'worksheets/sheet{i}.xml'})
        ET.SubElement(types,'Override',{'PartName':f'/xl/worksheets/sheet{i}.xml','ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
        xml=ET.Element('worksheet',{'xmlns':ns}); data=ET.SubElement(xml,'sheetData'); rows={}
        for address,cell in sorted(sheet.cells.items(),key=lambda x:(int(re.search(r'\d+',x[0])[0]),x[0])):
            row=int(re.search(r'\d+',address)[0]); parent=rows.setdefault(row,ET.SubElement(data,'row',{'r':str(row)})) if row not in rows else rows[row]
            attrs={'r':address};value=cell.value
            if isinstance(value,str):attrs['t']='str' if cell.formula else 'inlineStr'
            if isinstance(value,bool):attrs['t']='b'
            c=ET.SubElement(parent,'c',attrs)
            if cell.formula or cell.formula_type:
                attrs=dict(cell.formula_attributes)
                if cell.formula_type in {'array','dataTable','shared'}:attrs['t']=cell.formula_type
                f=ET.SubElement(c,'f',attrs)
                if cell.formula:f.text=cell.formula.removeprefix('=')
            if isinstance(value,str) and not cell.formula:
                ET.SubElement(ET.SubElement(c,'is'),'t').text=value
            elif value is not None:ET.SubElement(c,'v').text=str(int(value)) if isinstance(value,bool) else str(value)
        parts[f'xl/worksheets/sheet{i}.xml']=ET.tostring(xml,encoding='utf-8',xml_declaration=True)
    parts['xl/workbook.xml']=ET.tostring(wb,encoding='utf-8',xml_declaration=True)
    parts['xl/_rels/workbook.xml.rels']=ET.tostring(relationships,encoding='utf-8',xml_declaration=True)
    parts['[Content_Types].xml']=ET.tostring(types,encoding='utf-8',xml_declaration=True)
    rootrels=ET.Element('Relationships',{'xmlns':'http://schemas.openxmlformats.org/package/2006/relationships'})
    ET.SubElement(rootrels,'Relationship',{'Id':'rId1','Type':rel+'/officeDocument','Target':'xl/workbook.xml'})
    parts['_rels/.rels']=ET.tostring(rootrels,encoding='utf-8',xml_declaration=True)
    path.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(path,'w',ZIP_DEFLATED) as archive:
        for name,value in sorted(parts.items()):
            entry=ZipInfo(name,date_time=(2026,11,4,0,0,0));entry.compress_type=ZIP_DEFLATED
            entry.external_attr=0o444 << 16
            archive.writestr(entry,value)
    path.chmod(0o444)


def detector_evaluation(workbook,gold,result,monitored):
    findings=result.get('findings',[]);found={f.get('code') for f in findings}; expected=set(gold['expected'])
    invalid=[];cache_mismatches=[]
    for finding in findings:
        for sheet,cell in refs(finding.get('evidence',[])):
            if sheet not in workbook.sheets or cell not in workbook.sheets[sheet].cells:invalid.append({'finding':finding.get('id'),'sheet':sheet,'cell':cell})
        for item in finding.get('calculation',{}).get('inputs',[]):
            if not isinstance(item,dict) or not {'sheet','cell','value'}<=item.keys():continue
            cell=workbook.sheets.get(item['sheet']);cell=cell.cells.get(item['cell']) if cell else None
            if cell is None or cell.value!=item['value']:cache_mismatches.append(item)
    proofs={}
    for code,core in gold.get('proof',{}).items():
        if code not in expected:continue
        candidates=[f for f in findings if f.get('code')==code]
        proofs[code]={'required':core,'complete_in_one_finding':any(refs(core)<=refs(f.get('evidence',[])) for f in candidates),
                      'best_source_count':max((len(refs(core)&refs(f.get('evidence',[]))) for f in candidates),default=0)}
    return {'expected_themes':sorted(expected),'detected_themes':sorted(found&set(monitored)),
            'true_positives':sorted(found&expected),'false_negatives':sorted(expected-found),
            'false_positives':sorted((found&set(monitored))-expected),'unscored_codes':sorted(found-set(monitored)),
            'raw_finding_count':len(findings),'finding_counts':dict(Counter(f.get('code') for f in findings)),
            'proof_core':proofs,'invalid_evidence':invalid,'source_cache_mismatches':cache_mismatches,
            'check_status_counts':dict(Counter(x.get('status') for x in result.get('checks',[])))}


def collect_payload(value,present=None,absent=None,candidate_ids=None):
    present=set() if present is None else present;absent=set() if absent is None else absent;candidate_ids=set() if candidate_ids is None else candidate_ids
    if isinstance(value,dict):
        if isinstance(value.get('sheet'),str) and isinstance(value.get('cell'),str):
            pair=value['sheet'],value['cell']
            if value.get('present') is False:absent.add(pair)
            elif 'value' in value and value.get('present') is not False:present.add(pair)
        for sheet,rows in value.get('cell_data',{}).items():
            for row in rows:
                if isinstance(row,list) and len(row)>=2:
                    (absent if len(row)>=4 and row[3] in {'blank','missing','absent'} else present).add((sheet,row[0]))
        for finding in value.get('validation_findings',[]):
            if isinstance(finding,dict) and isinstance(finding.get('id'),str):candidate_ids.add(finding['id'])
        for item in value.values():
            if isinstance(item,(dict,list)):collect_payload(item,present,absent,candidate_ids)
    elif isinstance(value,list):
        for item in value:collect_payload(item,present,absent,candidate_ids)
    return present,absent,candidate_ids


def request_evidence(transcript):
    shown,missing,candidates=set(),set(),set();direct,direct_missing=set(),set();tools=[];inspections=[]
    def question_bodies(value):
        found=set()
        if isinstance(value,dict):
            question=value.get('investigation_question')
            if isinstance(question,dict):found.add(json.dumps(question,sort_keys=True,ensure_ascii=False,allow_nan=False))
            for question in value.get('investigation_questions',[]):
                if isinstance(question,dict):found.add(json.dumps(question,sort_keys=True,ensure_ascii=False,allow_nan=False))
            for child in value.values():
                if isinstance(child,(dict,list)):found|=question_bodies(child)
        elif isinstance(value,list):
            for child in value:found|=question_bodies(child)
        return found
    turns=transcript.get('turns',[]);request_sets=[];request_questions=[]
    for turn in turns:
        request_present,request_absent,request_candidates=set(),set(),set();bodies=set()
        for message in turn.get('request',{}).get('messages',[]):
            if message.get('role') not in {'user','function'}:continue
            try:obj=json.loads(message.get('content',''))
            except (TypeError,ValueError):continue
            if isinstance(obj,dict) and 'untrusted_workbook_data' in obj:
                data=obj['untrusted_workbook_data'];collect_payload(data,request_present,request_absent,request_candidates)
                bodies|=question_bodies(data)
        request_sets.append((request_present,request_absent));shown|=request_present;missing|=request_absent;candidates|=request_candidates
        request_questions.append(bodies)
    for index,turn in enumerate(turns):
        if turn.get('event')=='automatic_tool':continue
        response=turn.get('response',{});call=response.get('choices',[{}])[0].get('message',{}).get('function_call',{})
        if isinstance(call,dict) and isinstance(turn.get('tool_result_raw'),dict):
            tools.append({'name':call.get('name'),'arguments':call.get('arguments')})
            future_present=set().union(*(x[0] for x in request_sets[index+1:]));future_absent=set().union(*(x[1] for x in request_sets[index+1:]))
            raw_present,raw_absent,_=collect_payload(turn['tool_result_raw'])
            direct|=raw_present&future_present;direct_missing|=raw_absent&future_absent
            question=turn['tool_result_raw'].get('investigation_question')
            if call.get('name')=='inspect_question' and isinstance(question,dict):
                future_bodies=set().union(*request_questions[index+1:])
                body_delivered=json.dumps(question,sort_keys=True,ensure_ascii=False,allow_nan=False) in future_bodies
                inspections.append((question,future_present,future_absent,body_delivered))
    delivered=[]
    for question,future_present,future_absent,body_delivered in inspections:
        required=refs(question.get('evidence',[]));available=future_present|future_absent;complete=bool(required) and required<=available
        delivered.append({'question_id':question.get('id'),'category':question.get('category'),
                          'required_source_count':len(required),'actually_sent_source_count':len(required&available),
                          'source_bundle_fully_sent':complete,
                          'question_metadata_delivered':body_delivered,
                          'issues':[deepcopy(x) for x in question.get('issues',[]) if body_delivered and refs(x.get('evidence',[]))<=available],
                          'components':[deepcopy(x) for x in question.get('components',[]) if body_delivered and refs([x.get('source'),x.get('label_source')])<=future_present]})
    return {'shown':shown,'missing':missing,'candidate_ids':candidates,'direct':direct&shown,'direct_missing':direct_missing&missing,'tools':tools,'inspections':delivered}


def local_arithmetic(expression, variables):
    """Independent bounded evaluator for recorded scalar audit calculations."""
    tree=ast.parse(expression,mode='eval')
    nodes=list(ast.walk(tree))
    if len(nodes)>100:raise ValueError('Expression too large')
    operators={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Pow:operator.pow}
    def calc(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):return node.value
        if isinstance(node,ast.Name) and node.id in variables:return variables[node.id]
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            return calc(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        if isinstance(node,ast.BinOp) and type(node.op) in operators:
            left,right=calc(node.left),calc(node.right)
            if isinstance(node.op,ast.Pow) and abs(right)>100:raise ValueError('Exponent too large')
            return operators[type(node.op)](left,right)
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='sum' and not node.keywords:
            return sum(calc(arg) for arg in node.args)
        raise ValueError('Unsupported recorded expression')
    result=calc(tree.body)
    if not near(result,result):raise ValueError('Nonfinite result')
    return result


def agent_evaluation(workbook,report,gold,transcript_dir):
    stage_sources={}
    for path in sorted(transcript_dir.glob('*/*-transcript.json')):
        stage_sources[path.name.removesuffix('-transcript.json')]=request_evidence(json.loads(path.read_text()))
    empty={'shown':set(),'missing':set(),'candidate_ids':set(),'direct':set(),'direct_missing':set(),'tools':[],'inspections':[]}
    assumptions=stage_sources.get('assumptions',empty);all_shown=set().union(*(x['shown'] for x in stage_sources.values())) if stage_sources else set()
    issues=[];canonical=[];numeric=[];registry={(x.get('stage_id'),x.get('id')):x for x in report.get('facts',[])}
    for (stage,identity),card in registry.items():
        cell=workbook.sheets.get(card.get('source_sheet'));cell=cell.cells.get(card.get('value_cell')) if cell else None
        good=cell is not None and cell.value==card.get('value') and cell.formula==card.get('formula')
        numeric.append({'stage':stage,'fact_id':identity,'value_formula_match':good})
        if not good:issues.append({'type':'fact_numeric_source_mismatch','stage':stage,'fact_id':identity})
        for label,address in [('metric_label','metric_cell'),('unit','unit_cell')]:
            if card.get(address):
                source=workbook.sheets[card['source_sheet']].cells.get(card[address]);match=source is not None and source.value==card.get(label)
                if not match:issues.append({'type':'raw_context_mismatch','fact_id':identity,'field':label})
    calculation_checks=[]
    for calculation in report.get('calculations',[]):
        try:
            arguments=calculation['arguments'];variables={item['name']:workbook.sheets[item['sheet']].cells[item['cell']].value for item in arguments['inputs']}
            expected=local_arithmetic(arguments['expression'],variables);actual=calculation['result']['result']['value'];good=near(actual,expected)
            source_good=all(workbook.sheets[x['sheet']].cells[x['cell']].value==x['value'] and workbook.sheets[x['sheet']].cells[x['cell']].formula==x.get('formula') for x in calculation['result'].get('evidence',[]))
            calculation_checks.append({'expression':arguments['expression'],'published':actual,'independent':expected,'passed':good and source_good})
            if not good or not source_good:issues.append({'type':'calculation_numeric_mismatch','expression':arguments['expression']})
        except (KeyError,TypeError,ValueError,ArithmeticError,SyntaxError) as exc:
            calculation_checks.append({'status':'unsupported_evaluator_expression','error':type(exc).__name__})
    observations=[];structured=[];investigation_notes=[];candidate_ids=set().union(*(x['candidate_ids'] for x in stage_sources.values())) if stage_sources else set()
    provided={f['code']:f['id'] in candidate_ids for f in report.get('financial_checks',{}).get('findings',[])}
    for stage in report.get('stages',[]):
        evidence=stage_sources.get(stage['id'],empty)
        for index,obs in enumerate(stage.get('observations',[])):
            cited=refs(obs.get('evidence',[]))
            method=obs.get('methodology',{})
            source_records=method.get('source_cells',[])
            claimed_absent=refs([x for x in source_records if x.get('present') is False])
            if obs.get('origin')=='deterministic_investigation':claimed_absent |= evidence['missing']
            for sheet,address in cited:
                if (sheet not in workbook.sheets or address not in workbook.sheets[sheet].cells) and (sheet,address) not in claimed_absent&evidence['missing']:
                    issues.append({'type':'nonexistent_observation_source','stage':stage['id'],'index':index,'sheet':sheet,'cell':address})
            if method:
                mismatches=[]
                for item in source_records:
                    sheet=workbook.sheets.get(item.get('sheet'));cell=sheet.cells.get(item.get('cell')) if sheet else None
                    good=(cell is None) if item.get('present') is False else cell is not None and cell.value==item.get('value') and cell.formula==item.get('formula')
                    if not good:mismatches.append(item)
                if mismatches:issues.append({'type':'methodology_source_mismatch','stage':stage['id'],'index':index,'sources':mismatches})
                structured.append({'stage':stage['id'],'index':index,'origin':method.get('origin'),'finding_id':method.get('finding_id'),
                    'source_count':len(source_records),'source_records_match':not mismatches,
                    'all_sources_actually_sent':refs(source_records)<=evidence['shown']|evidence['missing'],
                    'verification':method.get('verification'),'all_periods':method.get('scope',{}).get('all_periods'),
                    'independent_discovery_claimed':False})
            if obs.get('origin')=='deterministic_investigation':
                investigation_notes.append({'question_id':obs.get('investigation_question_id'),'status':obs.get('status'),
                    'origin':'deterministic_investigation','all_sources_actually_sent':cited<=evidence['shown']|evidence['missing'],
                    'independent_discovery_claimed':False})
            for claim in obs.get('fact_claims',[]):
                card=registry.get((stage['id'],claim.get('fact_id')))
                fields=('value','value_cell','metric_label','period','unit','source_sheet')
                good=card is not None and all(claim.get(k)==card.get(k) for k in fields)
                canonical.append(good)
                if not good:issues.append({'type':'canonical_claim_mismatch','stage':stage['id'],'index':index})
            comparison=obs.get('comparison')
            if isinstance(comparison,dict):
                inp=comparison.get('inputs',[])
                try:
                    a,b=inp;av=workbook.sheets[a['sheet']].cells[a['cell']].value;bv=workbook.sheets[b['sheet']].cells[b['cell']].value
                    kind=comparison['kind'];expected=bv-av if kind=='difference' else bv/av-1 if kind=='total_growth' else (bv/av)**(1/comparison['periods']['years'])-1
                    display=expected if kind=='difference' else expected*100
                    good=near(comparison['result']['value'],expected) and near(comparison['display']['value'],display)
                except (ValueError,KeyError,TypeError,ArithmeticError):good=False
                if not good:issues.append({'type':'comparison_numeric_mismatch','stage':stage['id'],'index':index})
            if stage['id'] not in {'audit','quality'}:continue
            text=obs.get('title','')+' '+obs.get('detail','')
            for code,core in gold.get('proof',{}).items():
                # Heuristic topic assignment is explicitly not semantic grading.
                exact_code=method.get('original_finding',{}).get('code')
                if (exact_code==code or (not exact_code and re.search(TOPICS[code],text,re.I) and cited&refs(core))):
                    observations.append({'stage':stage['id'],'index':index,'title':obs.get('title'),'topic':code,
                        'matching':'immutable_finding_code' if exact_code else 'fixed_topic_regex_plus_source_intersection','required_core':core,'cited_core':addresses(cited&refs(core)),
                        'missing_core':addresses(refs(core)-cited),'core_complete':refs(core)<=cited,
                        'all_cited_actually_sent':cited<=evidence['shown'],'detector_candidate_was_supplied':provided.get(code,False),
                        'origin_claim':'candidate_assisted_or_restatement' if provided.get(code,False) else 'independent_origin_not_established',
                        'directly_examined_core_count':len(refs(core)&evidence['direct'])})
    questions=[]
    for question in gold.get('questions',[]):
        required=refs(question['required_present']);absent=refs(question.get('required_absent',[]))
        questions.append({'topic':question['topic'],'required_present':question['required_present'],'required_absent':question.get('required_absent',[]),
            'required_source_count':len(required)+len(absent),'directly_examined_source_count':len(required&assumptions['direct'])+len(absent&assumptions['direct_missing']),
            'available_in_assumptions_requests':addresses(required&assumptions['shown']),
            'directly_examined_in_assumptions':addresses(required&assumptions['direct']),
            'explicit_absence_examined':addresses(absent&assumptions['direct_missing']),
            'fully_examined_in_assumptions':required<=assumptions['direct'] and absent<=assumptions['direct_missing'],
            'available_any_stage':addresses(required&all_shown),'status':'sources_examined' if required<=assumptions['direct'] and absent<=assumptions['direct_missing'] else 'not_examined_or_incomplete'})
    return {'provenance_scope':'Actual serialized requests plus explicitly executed nonautomatic tools; planning/primer is not investigation.',
            'card_count':len(numeric),'card_value_formula_matches':sum(x['value_formula_match'] for x in numeric),'canonical_claim_count':len(canonical),'canonical_claim_matches':sum(canonical),
            'numeric_or_source_issues':issues,'calculation_checks':calculation_checks,'critical_questions':questions,'methodological_observations':observations,
            'structured_methodology':structured,'deterministic_investigation_observations':investigation_notes,
            'delivered_investigation_bundles':assumptions['inspections'],
            'declared_investigation':next((s.get('investigation') for s in report.get('stages',[]) if s.get('id')=='assumptions'),None),
            'proof_summary':{stage:{'expected_topics':gold['expected'],'matched_topics':sorted({o['topic'] for o in observations if o['stage']==stage}),'complete_core_topics':sorted({o['topic'] for o in observations if o['stage']==stage and o['core_complete']})} for stage in ('audit','quality')},
            'assumptions_direct_tools':assumptions['tools'],'usage':report.get('usage'),
            'completion':{s['id']:s.get('completion') for s in report.get('stages',[])},
            'limitations':['Topic matching is heuristic; short citations can be incomplete while a separate deterministic finding retains full proof.','No absence of a numeric mismatch is a certificate for all free-text financial claims.','No live agent rerun or external assumption verification occurs in this evaluator.']}


def optional_investigation(workbook,gold):
    spec=importlib.util.find_spec('finmodel_audit.investigation')
    if spec is None:return {'status':'not_available_in_runtime_snapshot'}
    try:
        from finmodel_audit.investigation import build_investigation,inspect_question,investigation_coverage
        plan=build_investigation(workbook)
        questions=plan.get('questions',[])
        inspected=[inspect_question(workbook,q['id'],plan=plan) for q in questions]
        present,absent,_=collect_payload(inspected)
        coverage=[]
        for q in gold.get('questions',[]):
            need,missing=refs(q['required_present']),refs(q.get('required_absent',[]))
            coverage.append({'topic':q['topic'],'required':len(need)+len(missing),'present':len(need&present),'explicit_absent':len(missing&absent),
                             'complete':need<=present and missing<=absent,'missing':addresses(need-present),'missing_absence_proof':addresses(missing-absent)})
        return {'status':'offline_code_inspection_only','plan':plan,'inspection_results':inspected,'evaluation':investigation_coverage(plan,inspected),'gold_source_coverage':coverage,'agent_examination_claimed':False}
    except Exception as exc:return {'status':'evaluation_adapter_error','error_type':type(exc).__name__,'message':str(exc)[:200]}


def evaluate(args):
    destination=Path(args.output_dir).resolve()
    if (destination/'evaluation.json').exists():raise FileExistsError('Refusing to overwrite completed evaluation')
    runtime=Path(args.runtime_dir).resolve();sys.path.insert(0,str(runtime));sys.path.insert(0,str(ROOT/'tests'))
    from finmodel_audit.financial import run_financial_checks
    from finmodel_audit.workbook import load_workbook
    from quality_cases.builders import build_case
    catalog=json.loads((CASES/'catalog.json').read_text()); originals=json.loads((CASES/'originals.json').read_text())['books']
    overrides={}
    for value in getattr(args,'report',[]):
        identity,separator,path=value.partition('=')
        if not separator or identity not in {g['id'] for g in originals} or identity in overrides:
            raise ValueError('--report must be a unique original ID=PATH binding')
        overrides[identity]=Path(path).resolve()
    manifest={'evaluator_sha256':sha(Path(__file__)),'label_version':catalog['version'],'split':args.split,'runtime_dir':str(runtime.relative_to(ROOT)) if runtime.is_relative_to(ROOT) else str(runtime),
              'runtime_sha256':{str(p.relative_to(runtime)):sha(p) for p in sorted((runtime/'finmodel_audit').rglob('*.py'))},
              'labels_sha256':{str(p.relative_to(ROOT)):sha(p) for p in sorted(CASES.glob('*')) if p.is_file()},'api_calls':0,'originals_written':False}
    records=[];started=time.monotonic()
    for spec in catalog['cases']:
        if args.split!='all' and spec['split']!=args.split:continue
        wb,truth=build_case(spec);fixture=destination/'synthetic'/f"{spec['id']}.xlsx";write_fixture(wb,fixture);parsed=load_workbook(fixture)
        result=run_financial_checks(parsed);gold={**spec,**truth}
        record={'id':spec['id'],'split':spec['split'],'fixture_sha256':sha(fixture),'source_cell_count':sum(len(s.cells) for s in parsed.sheets.values()),
                'detector':detector_evaluation(parsed,gold,result,catalog['monitored_codes']),'investigation':optional_investigation(parsed,gold)}
        write_json(destination/'cases'/f"{spec['id']}.json",{'metadata':record,'detector_raw':result,'gold':gold});records.append(record)
    original_records=[]
    if not args.no_originals:
        for gold in originals:
            path=ROOT/gold['source']
            if not path.exists():original_records.append({'id':gold['id'],'status':'source_missing'});continue
            before=sha(path)
            if before!=gold['sha256']:raise ValueError('Original SHA differs: '+gold['id'])
            wb=load_workbook(path);raw=run_financial_checks(wb);report_path=overrides.get(gold['id'],ROOT/gold['report']);report=json.loads(report_path.read_text());report_before=sha(report_path)
            agent=agent_evaluation(wb,report,gold,report_path.parent/'checkpoints')
            record={'id':gold['id'],'status':'evaluated','sha256':before,'source_unchanged':sha(path)==before,'report_path':str(report_path),'report_sha256':report_before,'report_unchanged':sha(report_path)==report_before,
                    'detector':detector_evaluation(wb,gold,raw,catalog['monitored_codes']),'stored_agent':agent,'investigation':optional_investigation(wb,gold)}
            write_json(destination/'originals'/f"{gold['id']}.json",{'metadata':record,'detector_raw':raw});original_records.append(record)
    by_split={}
    for split in ('development','held_out'):
        selected=[r for r in records if r['split']==split]
        by_split[split]={'cases':len(selected),'positive_themes':sum(len(r['detector']['expected_themes']) for r in selected),
            'true_positive_themes':sum(len(r['detector']['true_positives']) for r in selected),'false_negative_themes':sum(len(r['detector']['false_negatives']) for r in selected),
            'false_positive_themes':sum(len(r['detector']['false_positives']) for r in selected),'clean_cases':sum(not r['detector']['expected_themes'] for r in selected),
            'clean_cases_with_false_positive':sum(not r['detector']['expected_themes'] and bool(r['detector']['false_positives']) for r in selected)}
    payload={'manifest':manifest,'synthetic_cases':records,'originals':original_records,'summary':{'synthetic':by_split,'elapsed_seconds':time.monotonic()-started},
             'limitations':['Theme counts deduplicate repeated findings and are not materiality scores.','False positives are measured only for explicitly monitored labels; new unscored codes are reported separately.','Two original books are selected development controls, not a representative population.','Changing deterministic runtime does not create new agent evidence; report paths and hashes identify the actual stored run.','Held-out transformations are fixed evaluation variants, not permission to tune fixture-specific addresses.']}
    write_json(destination/'evaluation.json',payload)
    print(json.dumps(payload['summary'],ensure_ascii=False))
    return payload


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--runtime-dir',default=str(ROOT/'src'))
    parser.add_argument('--split',choices=['development','held_out','all'],default='development')
    parser.add_argument('--no-originals',action='store_true')
    parser.add_argument('--report',action='append',default=[],metavar='ID=PATH',help='Override a stored live report without changing frozen labels; repeat for each original. Checkpoints must be beside report.json.')
    args=parser.parse_args();evaluate(args)


if __name__=='__main__':main()
