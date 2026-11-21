import React from 'react';
import {render, screen, fireEvent, cleanup, within} from '@testing-library/react';
import {afterEach, beforeEach, expect, test, vi} from 'vitest';
import {ReportView} from './App.jsx';
import {ComparisonCharts, SensitivityCharts, KeyMetrics, isShownFactDuplicate} from './PresentationView.jsx';
import {chartDomain} from './ReportCharts.jsx';

beforeEach(()=>vi.stubGlobal('ResizeObserver',class {constructor(callback){this.callback=callback;} observe(target){this.callback([{target,contentRect:{width:640,height:230}}]);} unobserve(){} disconnect(){}}));
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();});

const fact={id:'revenue-early',stage_id:'business',source_sheet:'Model <source>',value_cell:'D17',value:100,
  metric_label:'Revenue',metric_cell:'B17',metric_key:'revenue',period:2020,period_cell:'D3',
  period_type:null,period_type_cell:null,unit:'USD k',unit_cell:'B4',binding_status:'bound',issues:[]};
const canonical={title:'Revenue',detail:'Canonical saved fact.',narrative_verification:'source_fact_text_generated',
  status:'observation',impact:'not_assessed',fact_claims:[{fact_id:fact.id,...Object.fromEntries(['metric_label','period','unit','source_sheet','value','value_cell','binding_status'].map(key=>[key,fact[key]]))}],evidence:[{sheet:fact.source_sheet,cell:fact.value_cell}]};

function fixture(){return {
  stages:[{id:'audit',title:'Аудит расчётов',summary:'Расчёты требуют проверки',status:'limited'},
    {id:'assumptions',title:'Предпосылки',summary:'Предпосылки'}, {id:'quality',title:'Качество модели',summary:'Качество'},
    {id:'business',title:'Обзор бизнеса',summary:'По сохранённым данным',observations:[canonical,{title:'Отдельное наблюдение',detail:'Не дублируется',status:'not_verified'}]}],facts:[fact],
  supplemental_audit:{status:'partial',findings:[],limitations:['Часть формул не поддержана.']},
  presentation:{version:'2026-11-04.1',overview:{title:'Результаты и вопросы для проверки',status:'limited',summary:['Отчёт сформирован. Пригодность не установлена.'],next_steps:[{text:'Проверить состав FCFF.',evidence:[{sheet:'DCF',cell:'B25'}]}]},
    metrics:[{fact_id:fact.id,label:'Revenue',value:100,value_label:'100',unit:'USD k',period:2020,period_label:'2020',period_type:null,binding_status:'bound',source_sheet:fact.source_sheet,value_cell:'D17',evidence:[{sheet:fact.source_sheet,cell:'D17'}]}],
    stage_views:{business:{fact_ids:[fact.id],collapsed_observation_indexes:[0]}},financial_groups:[],comparison_charts:[],sensitivity_charts:[],notices:[]}
};}

test('overview keeps raw percent values separate from unit labels and unknown periods explicit',()=>{
  const select=vi.fn();const report=fixture();
  report.presentation.metrics=[{...report.presentation.metrics[0],label:'Rate',value:.08,value_label:'0,08',unit:'%',period:null,period_label:null,binding_status:'partial'}];
  render(<ReportView report={report} onEvidence={select}/>);
  const metrics=within(screen.getByRole('region',{name:'Ключевые факты'}));
  expect(metrics.getByText('0,08')).toBeTruthy();
  expect(metrics.getByText('Подпись единицы:').textContent).toContain('%');
  expect(metrics.getByText('Период не определён')).toBeTruthy();
  expect(metrics.getByText('Факт/прогноз не определён')).toBeTruthy();
  expect(metrics.queryByText(/0,08\s*%/)).toBeNull();
  expect(metrics.queryByText('8%')).toBeNull();
  fireEvent.click(metrics.getByRole('button',{name:/^Сохранённое значение:/}));
  expect(select).toHaveBeenLastCalledWith({sheet:fact.source_sheet,cell:'D17'});
  expect(screen.getByRole('region',{name:'Обзор результатов'})).toBeTruthy();
});

test('four sticky tabs expose keyboard navigation and linked panels; technical checks start collapsed',()=>{
  const {container}=render(<ReportView report={fixture()} onEvidence={()=>{}}/>);
  const tabs=screen.getAllByRole('tab');expect(tabs).toHaveLength(4);
  expect(tabs[0].getAttribute('aria-selected')).toBe('true');
  tabs[0].focus();fireEvent.keyDown(tabs[0],{key:'End'});
  expect(document.activeElement).toBe(tabs[3]);expect(tabs[3].getAttribute('aria-selected')).toBe('true');
  expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe(tabs[3].id);
  fireEvent.keyDown(tabs[3],{key:'ArrowRight'});expect(document.activeElement).toBe(tabs[0]);
  fireEvent.keyDown(tabs[0],{key:'ArrowLeft'});expect(document.activeElement).toBe(tabs[3]);
  fireEvent.keyDown(tabs[3],{key:'Home'});expect(document.activeElement).toBe(tabs[0]);
  for(const tab of tabs)expect(document.getElementById(tab.getAttribute('aria-controls'))).toBeTruthy();
  const disclosure=screen.getByText('Дополнительные проверки Excel · Выполнены частично').closest('details');
  expect(disclosure.open).toBe(false);
  fireEvent.click(disclosure.querySelector('summary'));
  expect(disclosure.open).toBe(true);
  expect(container.querySelector('.report-tabs')).toBeTruthy();
});

test('only identical canonical facts in the visible stage table collapse, originals stay available',()=>{
  const report=fixture();
  report.stages[3].observations.push({...canonical,detail:'Changed source value.',fact_claims:[{...canonical.fact_claims[0],value:999}]});
  report.presentation.stage_views.business.collapsed_observation_indexes=[0,1,2];
  const {container,rerender}=render(<ReportView report={report} onEvidence={()=>{}}/>);
  fireEvent.click(screen.getByRole('tab',{name:/Обзор бизнеса/}));
  const originals=container.querySelector('.original-observations');
  expect(originals.open).toBe(false);expect(within(originals).getByText('Canonical saved fact.')).toBeTruthy();
  expect(screen.getByText('Changed source value.').closest('.original-observations')).toBeNull();
  expect(screen.getByText('Не дублируется').closest('.original-observations')).toBeNull();
  fireEvent.click(within(originals).getByText('Исходные наблюдения к показанным фактам · 1'));
  expect(originals.open).toBe(true);
  const hiddenFactReport=structuredClone(report);hiddenFactReport.presentation.stage_views.business.fact_ids=[];
  rerender(<ReportView report={hiddenFactReport} onEvidence={()=>{}}/>);
  expect(screen.getByText('Canonical saved fact.').closest('.original-observations')).toBeNull();
  expect(within(screen.getByRole('tabpanel')).queryByRole('region',{name:'Данные из модели'})).toBeNull();
  const appendix=screen.getByText('Все сохранённые факты · 1').closest('details');
  expect(appendix.open).toBe(false);expect(within(appendix).getByText('Revenue')).toBeTruthy();
  expect(isShownFactDuplicate({...canonical,comparison:{}},[fact])).toBe(false);
});

function comparisonChart(){return {id:'comparison-1',title:'Revenue',metric_label:'Revenue',source_sheet:'Financials',unit:'USD k',
  points:[{label:'2020',period:2020,period_type:null,value:-50,sheet:'Financials',cell:'D17'},
    {label:'2024',period:2024,period_type:null,value:0,sheet:'Financials',cell:'H17'}],
  comparisons:[{kind:'difference',display:{value:50,unit:'USD k'}}],evidence:[],note:'Только две исходные точки.'};}

test('endpoint bars preserve negative and zero values without drawing an interpolated trajectory',()=>{
  const select=vi.fn();const {container,rerender}=render(<ComparisonCharts items={[comparisonChart()]} onEvidence={select} renderDetails={()=>null}/>);
  expect(screen.getAllByText('Факт/прогноз не определён')).toHaveLength(2);
  const table=within(screen.getByRole('table'));
  expect(table.getByRole('button',{name:/2020: -50/})).toBeTruthy();
  fireEvent.click(table.getByRole('button',{name:/2024: 0/}));expect(select).toHaveBeenCalledWith({sheet:'Financials',cell:'H17'});
  expect(container.querySelectorAll('.recharts-line')).toHaveLength(0);
  expect(container.querySelector('.recharts-bar')).toBeTruthy();
  expect(chartDomain([-50,0])[0]).toBeLessThan(-50);expect(chartDomain([-50,0])[1]).toBe(0);
  expect(chartDomain([-10,20])).toEqual([-12.4,22.4]);
  const missingPeriod=comparisonChart();missingPeriod.points[0].period=null;
  const nonfinite=comparisonChart();nonfinite.points[0].value=NaN;
  rerender(<ComparisonCharts items={[missingPeriod,nonfinite]} onEvidence={select} renderDetails={()=>null}/>);
  expect(screen.queryByRole('figure')).toBeNull();
});

test('comparison percentages use the typed display value once and never rescale raw endpoints',()=>{
  const chart=comparisonChart();chart.unit='%';chart.points[0].value=.08;chart.points[1].value=.12;
  chart.comparisons=[{kind:'cagr',display:{value:10.66819197,unit:'% в год'}}];
  render(<ComparisonCharts items={[chart]} onEvidence={()=>{}} renderDetails={()=>null}/>);
  const table=within(screen.getByRole('table'));
  expect(table.getByRole('button',{name:/2020: 0,08/})).toBeTruthy();
  expect(table.getByRole('button',{name:/2024: 0,12/})).toBeTruthy();
  expect(screen.getByText('10,6682')).toBeTruthy();expect(screen.getByText('% в год')).toBeTruthy();
  expect(screen.queryByText('1 066,8192')).toBeNull();
});

test('sensitivity groups keep signed deltas and a separate baseline with clickable output sources',()=>{
  const select=vi.fn();const chart={id:'rate',title:'Ставка дисконтирования',target:{sheet:'DCF',cell:'G41'},unit:'USD k',baseline:100,
    points:[{label:'-1 п.п.',value:110,delta:10,change_pp:-1},{label:'+1 п.п.',value:90,delta:-10,change_pp:1}],scenarios:[],evidence:[],note:'FCFF зафиксированы.'};
  const {container}=render(<SensitivityCharts items={[chart]} onEvidence={select} renderScenarios={()=>null}/>);
  const table=within(screen.getByRole('table'));
  expect(table.getByText('+10')).toBeTruthy();expect(table.getByText('-10')).toBeTruthy();
  expect(table.getByText('-1 п.п.')).toBeTruthy();expect(table.getByText('+1 п.п.')).toBeTruthy();
  expect(screen.getByText('Выход при исходных входах').parentElement.textContent).toContain('100');
  fireEvent.click(screen.getByRole('button',{name:'DCF!G41'}));expect(select).toHaveBeenCalledWith({sheet:'DCF',cell:'G41'});
  expect(container.textContent).not.toContain('Исправленный');expect(container.querySelector('.recharts-bar')).toBeTruthy();
});

test('presentation groups retain every original while raw labels remain escaped and severity is not inferred',()=>{
  const attack='<img src=x onerror=alert(1)>';const report=fixture();
  const originals=[1,2].map(index=>({id:String(index),title:`Original ${index}`,detail:attack,status:'candidate',evidence:[{sheet:attack,cell:`A${index}`}],origin_metadata:{severity:'critical'}}));
  report.presentation.financial_groups=[{id:'fcff',code:'duplicate',title:attack,detail:'Два случая одной проверки.',consequence:'Проверить состав.',action:'Проверить источники.',status:'candidate',count:2,evidence:originals.flatMap(item=>item.evidence),findings:originals}];
  report.presentation.metrics[0].label=attack;
  const {container}=render(<ReportView report={report} onEvidence={()=>{}}/>);
  expect(container.querySelector('img,script')).toBeNull();expect(container.textContent).toContain(attack);
  expect(container.textContent).not.toContain('critical');
  const group=container.querySelector('.finding-group');expect(group).toBeTruthy();
  const details=within(group).getByText('Исходные замечания · 2').closest('details');expect(details.open).toBe(false);
  fireEvent.click(details.querySelector('summary'));
  expect(within(details).getByText('Original 1')).toBeTruthy();expect(within(details).getByText('Original 2')).toBeTruthy();
  expect(within(group).getAllByText('Требует проверки')).toHaveLength(3);
});
