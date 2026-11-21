import React from 'react';
import {render,screen,fireEvent,cleanup,within} from '@testing-library/react';
import {afterEach,expect,test,vi} from 'vitest';
import ImpactPanel,{impactScenarioIssue} from './ImpactPanel.jsx';
import {ReportView} from './App.jsx';

afterEach(cleanup);
const cell=(cell,sheet='DCF')=>({sheet,cell});
function scenario(overrides={}) {
  return {id:'conditional-1',title:'Убрать одно повторное слагаемое',assumption:'Одно вхождение потока исключено только из суммы.',
    target:cell('C9'),raw_unit:'RUB M',baseline:100,saved_output:100,baseline_cache_difference:0,result:70,delta:-30,relative_delta:-.3,
    patches:[{...cell('C8'),original_formula:'=A1+B1+B1',changed_formula:'=A1+B1',excluded:[cell('B1')]}],
    formula_path:[{...cell('C8'),formula:'=A1+B1+B1'},{...cell('C9'),formula:'=C8'}],
    source_inputs:[{...cell('A1'),value:40,frozen:true},{...cell('B1'),value:30,frozen:true}],
    evidence:['A1','B1','C8','C9'].map(value=>cell(value)),conditional:true,recalculated_workbook:false,status:'calculated',
    limitations:['Другие вхождения потока сохранены.'],...overrides};
}

test('conditional impact shows a signed separate scenario with exact formula and source navigation',()=>{
  const onEvidence=vi.fn(); const item=scenario();
  const {container}=render(<ImpactPanel data={{status:'limited',scenarios:[item],checks:[],limitations:['Сценарии не суммируются.']}} onEvidence={onEvidence}/>);
  const table=screen.getByRole('table',{name:'Отдельные условные сценарии'});
  const row=within(table).getByRole('rowheader',{name:item.title}).parentElement;
  expect(row.textContent).toContain('10070-30RUB M');
  fireEvent.click(within(row).getByRole('button',{name:'DCF!C9'}));
  expect(onEvidence).toHaveBeenLastCalledWith(cell('C9'));
  fireEvent.click(screen.getByText('Допущение, изменённые формулы и зафиксированные входы'));
  expect(container.querySelector('.check-calculation').closest('.table-scroll')).toBeNull();
  expect(container.querySelector('.check-calculation summary').textContent).toContain(item.title);
  expect(screen.getByText('=A1+B1')).toBeTruthy();
  expect(screen.getByText(/Относительное изменение к модулю исходного расчёта: -30%/)).toBeTruthy();
  const input=container.querySelector('.sensitivity-input');
  fireEvent.click(within(input).getByRole('button',{name:'DCF!A1'}));
  expect(onEvidence.mock.calls.at(-1)[0]).toMatchObject(cell('A1'));
  expect(container.textContent).toContain('Эффекты отдельных сценариев не складываются');
  expect(container.textContent).toContain('Книга Excel целиком не пересчитывалась');
  expect(screen.getByRole('region',{name:'Таблица условных сценариев'}).tabIndex).toBe(0);
});

test('negative base uses absolute denominator and zero base has no invented percentage',()=>{
  const {container,rerender}=render(<ImpactPanel data={{scenarios:[scenario({baseline:-100,saved_output:-100,result:-70,delta:30,relative_delta:.3})]}}/>);
  expect(container.textContent).toContain('+30%');
  rerender(<ImpactPanel data={{scenarios:[scenario({baseline:0,saved_output:0,result:-10,delta:-10,relative_delta:null})]}}/>);
  expect(container.textContent).toContain('Относительное изменение не определено: исходная база равна нулю');
  expect(container.textContent).not.toContain('0%');
  expect(container.textContent).toContain('-10');
});

test('raw percent unit stays separate from saved numbers without automatic scaling',()=>{
  render(<ImpactPanel data={{scenarios:[scenario({raw_unit:'%',baseline:.08,saved_output:.08,result:.06,delta:-.02,relative_delta:-.25})]}}/>);
  const row=screen.getByRole('rowheader',{name:/Убрать одно/}).parentElement;
  expect([...row.querySelectorAll('td')].slice(0,4).map(td=>td.textContent)).toEqual(['0,08','0,06','-0,02','%']);
});

test('invalid arithmetic flags sources and missing proof suppress numeric result cells',()=>{
  const variants=[{delta:999999},{relative_delta:.8},{result:Infinity},{saved_output:null},{baseline_cache_difference:7},
    {conditional:false},{recalculated_workbook:true},{target:null},{source_inputs:[]},{evidence:[cell('C9')]},
    {patches:[{...cell('C8'),original_formula:'=A1+B1',changed_formula:'=A1',excluded:[cell('Z99')]}]},
    {patches:[{...cell('C8'),original_formula:'=A1+B1',changed_formula:'=A1',scenario_formula:'=B1'}]}];
  for(const override of variants) {
    const item=scenario(override); expect(impactScenarioIssue(item)).toBeTruthy();
    const {container,unmount}=render(<ImpactPanel data={{scenarios:[item]}}/>);
    expect(container.textContent).toContain('Расчёт не показан');
    expect(container.querySelector('td').colSpan).toBe(5);
    expect(container.textContent).not.toContain('999999');
    unmount();
  }
  expect(impactScenarioIssue(scenario({baseline:0,saved_output:0,result:0,delta:0,relative_delta:0}))).toBeTruthy();
});

test('source and formula injection remains text and unknown metadata is not dumped',()=>{
  const attack='<img src=x onerror="alert(1)">'; const item=scenario({title:attack,assumption:attack,raw_unit:attack,limitations:[attack],private_debug:'DO NOT DISPLAY'});
  for(const value of [item.target,...item.patches,...item.formula_path,...item.source_inputs,...item.evidence,...item.patches[0].excluded]) value.sheet=attack;
  item.patches[0].changed_formula=attack; item.formula_path[0].formula=attack;
  const {container}=render(<ImpactPanel data={{scenarios:[item],limitations:[attack]}}/>);
  expect(container.querySelector('img,script')).toBeNull();
  expect(container.textContent).toContain(attack);
  expect(container.textContent).not.toContain('DO NOT DISPLAY');
  expect(container.textContent).not.toContain('Расчёт не показан');
});

test('unsupported checks have a readable reason and missing legacy payload stays absent',()=>{
  const {container,rerender}=render(<ImpactPanel data={{status:'unsupported',scenarios:[],checks:[{status:'unsupported',reason:'baseline_intermediate_cache_mismatch',target:cell('C9')},{status:'unsupported',reason:'internal_unknown_code'}],limitations:['Нет надёжной базы.']}}/>);
  expect(container.textContent).toContain('Поддержанные условные сценарии не представлены');
  expect(container.textContent).toContain('Промежуточный расчёт не совпадает с сохранёнными значениями');
  expect(container.textContent).not.toContain('internal_unknown_code');
  expect(container.querySelector('table')).toBeNull();
  rerender(<ImpactPanel/>);expect(container.textContent).toBe('');
});

test('report shows code-generated impact once in audit for current and legacy presentation',()=>{
  for(const presentation of [undefined,{overview:{},metrics:[],financial_groups:[],stage_views:{}}]) {
    const report={presentation,stages:[{id:'audit',title:'Аудит',summary:'Проверка источников.'},{id:'business',title:'Обзор'}],impact_analysis:{status:'limited',scenarios:[scenario()]}};
    const {container,unmount}=render(<ReportView report={report} onEvidence={()=>{}}/>);
    expect(container.querySelectorAll('.impact-panel').length).toBe(1);
    expect(container.querySelector('.impact-panel').closest('details')).toBeNull();
    fireEvent.click(screen.getByRole('tab',{name:/Обзор бизнеса/}));
    expect(container.querySelector('.impact-panel')).toBeNull();
    fireEvent.click(screen.getByRole('tab',{name:/Аудит расчётов/}));
    expect(container.querySelectorAll('.impact-panel').length).toBe(1);
    unmount();
  }
});
