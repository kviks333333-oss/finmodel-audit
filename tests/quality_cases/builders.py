"""Independent small financial books with known structural truth.

These builders do not import a detector and never read/write user workbooks.
Formula/cell offsets are test-only, never application routing rules.
"""
from __future__ import annotations
from pathlib import Path
import math
from datetime import date, timedelta
import re


def build_case(spec):
    from finmodel_audit.workbook import Workbook, Sheet, Cell
    cells = {}
    base_sheet = spec.get('sheet', 'Example cash model')
    shift, columns = spec.get('row_shift', 0), spec.get('column_shift', 0)
    variant, alternate = spec['variant'], spec.get('labels') == 'alternate'
    def ref(address):
        m=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',address)
        col=0
        for char in m[1]: col=col*26+ord(char)-64
        col+=columns; letters=''
        while col: col,r=divmod(col-1,26); letters=chr(65+r)+letters
        return letters+str(int(m[2])+shift)
    def formula(text):
        def move(m):
            target=ref(m[2]+m[4]); letters=target.rstrip('0123456789'); row=target[len(letters):]
            return m[1]+letters+m[3]+row
        return re.sub(r'(?<![A-Za-z0-9_])(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]*)(?![A-Za-z0-9_])',move,text)
    def add(address,value,expr=None,attrs=None):
        attrs={k:formula(v) if k in {'ref','r1','r2'} else v for k,v in (attrs or {}).items()}
        addr=ref(address)
        cells[addr]=Cell(addr,value,formula(expr) if expr else None,'dataTable' if attrs else 'normal' if expr else None,formula_attributes=attrs)
    def v(address):return cells[ref(address)].value
    def refs(*addresses):return [{'sheet':base_sheet,'cell':ref(a)} for a in addresses]
    proof={};questions=[];extra={}
    if spec['family']=='corporate':
        rate,g=.12,.025
        add('A1','Year');add('D1',2023);add('E1',2025);add('F1',2026);add('H1','TV')
        add('A3','Дата оценки' if alternate else 'Valuation date');add('B3','2024-12-31')
        add('A4','WACC');add('B4',rate);add('A5','LTGR');add('B5',g)
        add('A7','Free cash flow' if alternate else 'FCFF');add('B7','RUB M')
        for c,flow in [('D',75),('E',100),('F',130)]:add(c+'7',flow)
        add('H7',130,'=F7')
        add('A9','Discount period')
        for c,t in [('D',0),('E',1),('F',2),('H',2)]:add(c+'9',t,'=F9' if c=='H' else None)
        add('A10','Discount factor')
        for c in 'DEFH':add(c+'10',1/(1+rate)**v(c+'9'),'=F10' if c=='H' else f'=1/(1+B4)^{c}9')
        add('A11','Present value of FCF' if alternate else 'Discounted FCFF')
        for c in 'DEFH':add(c+'11',v(c+'7')*v(c+'10'),f'={c}7*{c}10')
        include=['E','F']
        if variant in {'duplicate','equal_independent','constant_independent'}:include.append('H')
        if variant in {'historical','historical_double'}:include.insert(0,'D')
        if variant=='equal_independent':add('H7',130)
        if variant=='constant_independent':add('F7',130,'=65+65');add('H7',130,'=65+65')
        add('B11',sum(v(c+'11') for c in include),'=SUM('+','.join(c+'11' for c in include)+')')
        bad=variant in {'double_discount','historical_double','disabled_gordon'}
        base=v('F11') if bad else v('F7')
        tv=base*(1+g)/(rate-g)
        add('A14','GGM');add('B14',tv,'=F11*(1+B5)/(B4-B5)' if bad else '=F7*(1+B5)/(B4-B5)')
        add('A15','Exit multiple value');add('B15',900)
        weight=0 if variant=='disabled_gordon' else 1
        add('C14',weight);add('C15',1-weight)
        add('A16','TV');add('B16',tv*weight+900*(1-weight),'=B14*C14+B15*C15')
        add('A17','Discounted TV');add('B17',v('B16')*v('F10'),'=B16*F10')
        add('A18','Enterprise Value' if alternate else 'EV');add('C18','RUB M');add('D18',v('B11')+v('B17'),'=B11+B17')
        add('C20',v('D18'),'=D18');add('D20',g);add('E20',.03);add('C21',rate);add('C22',.13)
        add('D21',v('D18')-17 if variant=='grid_mismatch' else v('D18'),attrs={'ref':'D21:E22','dt2D':'1','dtr':'1','r1':'B5','r2':'B4'})
        add('E21',v('D18')+1);add('D22',v('D18')-1);add('E22',v('D18'))
        if spec.get('cross_sheet_alias'):
            alias_sheet="Input O'Brien"
            alias_address='B2'
            target="'"+base_sheet.replace("'","''")+"'!"+ref('F7')
            extra[alias_sheet]=Sheet(alias_sheet,cells={alias_address:Cell(alias_address,130,'='+target,'normal')})
            cells[ref('H7')].formula="='Input O''Brien'!B2"
        proof={
          'pv_fcff_duplicate_term':refs('B11','F11','H11','F7','H7','F10','H10'),
          'pv_fcff_historical_period':refs('B11','D11','D1','B3'),
          'gordon_double_discount':refs('B14','B16','B17','C14','F11','F10'),
          'datatable_basepoint_cache_mismatch':refs('D18','C20','D21','D20','C21','B4','B5')}
        questions=[{'topic':'discount_rate','required_present':refs('D18','B4','B5'),'required_absent':[]}]
    else:
        # A finite project without terminal value is a clean alternative, not a
        # defective corporate DCF merely because the last cashflow is finite.
        bad_inputs=variant=='critical_inputs'
        rf=0 if variant in {'critical_inputs','zero_riskfree'} else .04
        add('A3','Risk-free rate');add('C3',rf,'=J3' if bad_inputs else None)
        add('A4','Equity premium');add('C4',.05);add('A5','Beta');add('C5',1.1)
        add('A6','Specific risk');add('C6',.02);add('A7','Cost of equity');add('C7',rf+.05*1.1+.02,'=C3+C4*C5+C6')
        add('A8','Cost of debt');add('C8',.06);add('A9','Tax rate');add('C9',.25)
        add('A10','Equity share');add('C10',.7);add('A11','Debt share');add('C11',.3,'=1-C10')
        rate=v('C7')*.7+.06*(1-.25)*.3
        add('A12','WACC');add('B12','%');add('C12',rate,'=C7*C10+C8*(1-C9)*C11')
        add('A14','Period length months');add('C14',12)
        add('A15','Start date');add('A16','End date');add('A17','Period')
        # Serial dates stay raw; rows explicitly identify a calendar interval.
        for i,c in enumerate('CDEF'):
            add(c+'17',i)
            if c!='C':
                start=46023 if bad_inputs else (date(2025+i,1,1)-date(1899,12,30)).days
                end=start-1 if bad_inputs else (date(2026+i,1,1)-timedelta(days=1)-date(1899,12,30)).days
                add(c+'15',start,None if c=='D' else f'={chr(ord(c)-1)}16+1')
                add(c+'16',end,f'=EDATE({c}15,$J$14)-1' if bad_inputs else f'=EDATE({c}15,$C$14)-1')
        add('A20','EBIT');add('B20','тыс. руб.');add('A21','Tax');add('A22','Interest')
        add('A23','Чистая прибыль' if alternate else 'Net Profit');add('A24','NOPAT');add('A25','D&A');add('A26','CAPEX')
        add('A28','Свободный денежный поток' if alternate else 'FCFF');add('B28','тыс. руб.')
        add('C28',-100)
        overlap=variant in {'overlap','overlap_discounted_irr'}
        for c,ebit in [('D',60),('E',80),('F',100)]:
            add(c+'20',ebit);add(c+'21',-.25*ebit,f'=-{c}20*C9');add(c+'22',-2)
            add(c+'23',.75*ebit-2,f'={c}20+{c}21+{c}22');add(c+'24',.75*ebit,f'={c}20+{c}21')
            add(c+'25',10);add(c+'26',-4)
            expr=f'=SUM({c}23:{c}26)' if overlap else f'={c}24+{c}25+{c}26'
            amount=(v(c+'23') if overlap else 0)+v(c+'24')+6
            if variant=='independent_profit':
                add(c+'32',ebit);add(c+'33',-.25*ebit);add(c+'24',.75*ebit,f'={c}32+{c}33')
                expr=f'=SUM({c}23:{c}26)';amount=v(c+'23')+v(c+'24')+6
            add(c+'28',amount,expr)
        add('A29','Discount factor')
        add('A30','Discounted cash flow');add('B30','тыс. руб.')
        for c in 'CDEF':
            add(c+'29',1/(1+rate)**v(c+'17'),f'=1/(1+C12)^{c}17')
            add(c+'30',v(c+'28')*v(c+'29'),f'={c}28*{c}29')
        add('A36','NPV');add('B36','тыс. руб.');add('C36',sum(v(c+'30') for c in 'CDEF'),'=SUM(C30:F30)')
        discounted=variant in {'discounted_irr','overlap_discounted_irr'}
        flows=[v(c+('30' if discounted else '28')) for c in 'CDEF']
        low,high=-.9,10.
        for _ in range(100):
            middle=(low+high)/2
            if sum(x/(1+middle)**i for i,x in enumerate(flows))>0:low=middle
            else:high=middle
        add('A37','IRR');add('B37','%');add('C37',(low+high)/2,'=IRR(C30:F30)' if discounted else '=IRR(C28:F28)')
        proof={'irr_on_discounted_cashflows':refs('C37','C30','D30','E30','F30','D28','E28','F28','D29','E29','F29'),
               'fcff_net_profit_nopat_overlap':refs('D28','D23','D24','D20','D21','A23','A24')}
        questions=[{'topic':'rate_components','required_present':refs('C36','C12','C7','C8','C9','C10','C11','C3','C4','C5','C6'),'required_absent':refs('J3') if bad_inputs else []},
                   {'topic':'calendar','required_present':refs('C14','D15','D16','E15','E16','F15','F16','D17','F17'),'required_absent':refs('J14') if bad_inputs else []}]
    # Reverse insertion order in held-out cases to prevent iteration-order cues.
    if spec['split']=='held_out':cells=dict(reversed(list(cells.items())))
    wb=Workbook(Path('synthetic-'+spec['id']+'.xlsx'),{base_sheet:Sheet(base_sheet,cells=cells),**extra},[],[],{},[])
    return wb, {'proof':proof,'questions':questions,'output':refs('D18') if spec['family']=='corporate' else refs('C36')}
