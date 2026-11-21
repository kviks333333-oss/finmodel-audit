"""Test-only arithmetic; imports neither financial nor impact detectors."""
from copy import deepcopy
from pathlib import Path
import re

from quality_cases.builders import build_case
from finmodel_audit.workbook import Cell, Sheet, Workbook


def _base(variant, family='corporate'):
    wb,_=build_case({'id':'impact','family':family,'variant':variant,'split':'development'})
    return wb,next(iter(wb.sheets.values())).cells


def _move(wb,rows,columns,name):
    old=next(iter(wb.sheets));mapping={}
    def address(value):
        m=re.fullmatch(r'([A-Z]+)([0-9]+)',value);column=0
        for ch in m[1]:column=column*26+ord(ch)-64
        column+=columns;letters=''
        while column:column,x=divmod(column-1,26);letters=chr(65+x)+letters
        return letters+str(int(m[2])+rows)
    def formula(value):
        if not value:return value
        return re.sub(r'(?<![A-Za-z0-9_])(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]*)(?![A-Za-z0-9_])',lambda m:m[1]+address(m[2]+m[4]).rstrip('0123456789')+m[3]+re.search(r'\d+$',address(m[2]+m[4]))[0],value)
    for ref,cell in wb.sheets[old].cells.items():
        target=address(ref);mapping[target]=Cell(target,cell.value,formula(cell.formula),cell.formula_type,cell.data_type,cell.style_id,
            {k:formula(v)if k in {'ref','r1','r2'}else v for k,v in cell.formula_attributes.items()})
    wb.sheets={name:Sheet(name,cells=mapping)}
    return address


def historical_case(spec):
    wb,c=_base('historical');variant=spec['variant']
    if variant=='remote_header':
        # Put a new empty gap between the explicit year row and financial block.
        _move(wb,63,0,'Temporary');c=next(iter(wb.sheets.values())).cells
        for col in 'ADEFH':
            source=col+'64';cell=c.pop(source);cell.address=col+'7';c[cell.address]=cell
    elif variant=='fy_text':
        for col in 'DEF':c[col+'1'].value='FY '+str(c[col+'1'].value)
    elif variant=='amount_decoy':
        c['A6']=Cell('A6','Custom operating charge');c['D6']=Cell('D6',2028);c['E6']=Cell('E6',2029)
    elif variant=='indexes':
        c['A1'].value='Period number'
        for i,col in enumerate('DEF'):c[col+'1'].value=i
    elif variant=='unlabeled_amounts':c['A1'].value='Custom maintenance cost'
    elif variant=='future':
        for i,col in enumerate('DEF'):c[col+'1'].value=2027+i
    elif variant=='ambiguous_year_text':
        for col in 'DEF':c[col+'1'].value='FY 2022/2025'
    elif variant=='cross_sheet_year':
        # Applied after relocation below: exact raw header year plus alias proof.
        pass
    elif variant=='missing_valuation_date':c['B3'].value='Date not established'
    elif variant=='unstyled_serials':
        for i,col in enumerate('DEF'):c[col+'1'].value=45292+366*i
    elif variant=='iso_date_headers':
        for col in 'DEF':c[col+'1'].value=str(c[col+'1'].value)+'-12-31'
    elif variant=='year_sized_indexes':c['A1'].value='Discount period number'
    address=_move(wb,spec['row_shift'],spec['column_shift'],spec['sheet'])
    if variant=='cross_sheet_year':
        cells=wb.sheets[spec['sheet']].cells;alias="Calendar O'Brien"
        wb.sheets[alias]=Sheet(alias,cells={'C4':Cell('C4',2023)})
        cells[address('D1')].formula="='Calendar O''Brien'!C4";cells[address('D1')].formula_type='normal'
    return wb


def impact_case(spec):
    variant=spec['variant'];family='project' if variant.startswith('project') else 'corporate'
    base='overlap' if family=='project' else 'double_discount' if variant.startswith('gordon') else 'historical' if variant.startswith('historical') else 'duplicate'
    wb,c=_base(base,family);sheet=next(iter(wb.sheets));gold=[]
    if family=='corporate':
        for ref in ('C20','D20','E20','C21','C22','D21','E21','D22','E22'):c.pop(ref,None)
    if family=='project':
        if variant=='project_negative':
            c['C28'].value=-1000;c['C30'].value=-1000
        baseline=sum(c[col+'30'].value for col in 'CDEF');c['C36'].value=baseline
        for row,kind in [(23,'exclude_net_profit'),(24,'exclude_nopat')]:
            result=baseline-sum(c[col+str(row)].value*c[col+'29'].value for col in 'DEF')
            gold.append({'kind':kind,'code':'fcff_net_profit_nopat_overlap','target':{'sheet':sheet,'cell':'C36'},'baseline':baseline,'result':result,'scope':'all_identified_overlap_terms','required':False})
            for col in 'DEF':
                gold.append({'kind':kind,'code':'fcff_net_profit_nopat_overlap','target':{'sheet':sheet,'cell':'C36'},'baseline':baseline,
                    'result':baseline-c[col+str(row)].value*c[col+'29'].value,'scope':'one_overlap_formula','changed_cell':col+'28','removed_cell':col+str(row),'required':True})
    else:
        if variant in {'duplicate_negative','duplicate_zero'}:
            for col in 'FH':c[col+'7'].value=-130;c[col+'11'].value=-130*c[col+'10'].value
            c['B11'].value=c['E11'].value+c['F11'].value+c['H11'].value
            c['B14'].value=c['F7'].value*(1+c['B5'].value)/(c['B4'].value-c['B5'].value)
            c['B16'].value=c['B14'].value;c['B17'].value=c['B16'].value*c['F10'].value
        if variant=='historical_negative':c['D7'].value=-75;c['D11'].value=-75;c['B11'].value=sum(c[col+'11'].value for col in 'DEF')
        old_ev=c['B11'].value+c['B17'].value;c['D18'].value=old_ev
        adjustment=-old_ev if variant=='duplicate_zero' else 0
        scale=1000 if variant=='duplicate_scaled' else 1
        if adjustment:c['E18']=Cell('E18',adjustment);c['D18'].formula='=B11+B17+E18'
        if scale!=1:c['D18'].formula='=(B11+B17)*1000';c['C18'].value='RUB'
        baseline=(old_ev+adjustment)*scale;c['D18'].value=baseline
        if variant.startswith('gordon'):
            result=(c['B11'].value+c['F7'].value*(1+c['B5'].value)/(c['B4'].value-c['B5'].value)*c['F10'].value)*scale
            kind,code='gordon_single_discount','gordon_double_discount'
            if variant=='gordon_disabled':
                c['C14'].value=0;c['C15'].value=1;c['B16'].value=c['B15'].value;c['B17'].value=c['B15'].value*c['F10'].value;c['D18'].value=c['B11'].value+c['B17'].value
        else:
            removed=c['D11' if variant.startswith('historical') else 'H11'].value
            result=baseline-removed*scale
            kind,code=('drop_historical_pv','pv_fcff_historical_period')if variant.startswith('historical')else('drop_duplicate_pv','pv_fcff_duplicate_term')
        gold.append({'kind':kind,'code':code,'target':{'sheet':sheet,'cell':'D18'},'baseline':baseline,'result':result,'scope':'single_formula_occurrence'})
        if variant=='duplicate_cross_sheet':
            alias="Output O'Brien";formula="='"+sheet.replace("'","''")+"'!D18"
            c['A18'].value='Intermediate total';wb.sheets[alias]=Sheet(alias,cells={'A4':Cell('A4','EV'),'B4':Cell('B4',baseline,formula,'normal'),'C4':Cell('C4','RUB M')})
            gold[0]['target']={'sheet':alias,'cell':'B4'}
        if variant=='duplicate_nonlinear':
            c['D18'].formula='=(B11+B17)^2';c['D18'].value=baseline**2;gold[0].update(baseline=baseline**2,result=result**2)
        if variant=='duplicate_dynamic':c['D18'].formula='=INDIRECT("B11")+B17'
        if variant=='duplicate_external':c['D18'].formula="='[outside.xlsx]Inputs'!B11+B17"
        if variant=='independent_equal':c['H7'].formula=None;c['H7'].formula_type=None
    for item in gold:
        item['delta']=item['result']-item['baseline'];item['relative_delta']=item['delta']/abs(item['baseline'])if item['baseline'] else None
    return wb,gold
