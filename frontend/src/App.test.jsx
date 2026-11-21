import React from 'react';
import {render, screen, fireEvent, waitFor, cleanup, within} from '@testing-library/react';
import {vi, afterEach, test, expect} from 'vitest';
import App, {UploadPanel, CalculationPanel, ReportView, FactPanel, CheckPanel, ComparisonDetails, ValuationSensitivityPanel} from './App.jsx';
import {displayValue} from './api.js';
afterEach(()=>{cleanup();vi.restoreAllMocks();});

function sensitivityFixture(){return {status:'limited',scenarios:[{
  target:{sheet:'DCF',cell:'E55'},parameter:'discount_rate',changed_parameter:{sheet:'WACC',cell:'E42',baseline:.2,change:-.01,effective_value:.19,change_unit:'absolute_fraction'},
  raw_unit:'RUB M',baseline:100,saved_output:100,baseline_cache_difference:0,result:110,delta:10,relative_delta:.1,
  explicit_formula:'=E39+E41',formula_path:[{sheet:'DCF',cell:'E39',formula:'=SUM(H39:N39)'}],
  source_inputs:[{sheet:'Inputs',cell:'D27',value:0,formula:'=D10-D11',frozen:true}],
  periods:[{discount_cell:{sheet:'DCF',cell:'H37'},period_expression:'internal tuple',value:0,evidence:[{sheet:'DCF',cell:'H36'}]}],
  effective_parameters:[{sheet:'WACC',cell:'E42',value:.19,role:'discount_rate'}],frozen_cashflows:[{sheet:'DCF',cell:'H27'}],
  basis:'saved_unaffected_inputs_and_original_formula',recalculated_workbook:false,limitations:['Состав исходных потоков сохранён.']}],
  checks:[{target:{sheet:'Bridge',cell:'E14'},source_target:{sheet:'DCF',cell:'E55'},status:'not_applicable',reason:'alias_of_recognized_output'},
    {target:{sheet:'DCF',cell:'D50'},status:'unsupported',reason:'ambiguous_or_missing_discount_rate'}],limitations:['FCFF фиксированы; книга не пересчитывалась.']};}

test('valuation sensitivity shows bounded original-formula scenarios and routes evidence',()=>{
  const select=vi.fn();const {container}=render(<ValuationSensitivityPanel data={sensitivityFixture()} onEvidence={select}/>);
  expect(screen.getByText('Ставка дисконтирования · -1 п.п.')).toBeTruthy();
  expect(screen.getByText(/Параметр \(доля\): 0.2 → 0.19/)).toBeTruthy();
  expect(screen.getByText('Выход сценария').parentElement.textContent).toContain('110');
  expect(screen.getByText('Изменение выхода').parentElement.textContent).toContain('10');
  fireEvent.click(within(container.querySelector('.sensitivity-scenario')).getByRole('button',{name:'DCF!E55'}));expect(select).toHaveBeenLastCalledWith({sheet:'DCF',cell:'E55'});
  fireEvent.click(screen.getByText('Формулы, входы и периоды сценария'));
  expect(screen.getByText('=E39+E41')).toBeTruthy();
  expect(screen.getByText('Период: 0')).toBeTruthy();
  expect(screen.getByText(/Сохранённое значение: 0 · зафиксировано/)).toBeTruthy();
  fireEvent.click(within(container.querySelector('.sensitivity-input')).getByRole('button',{name:'Inputs!D27'}));
  expect(select.mock.calls.at(-1)[0]).toMatchObject({sheet:'Inputs',cell:'D27'});
  expect(container.textContent).not.toContain('internal tuple');
  fireEvent.click(screen.getByText('Охват и пропущенные выходы · 2'));
  expect(screen.getByText(/ссылка на уже рассмотренный выход/)).toBeTruthy();
  expect(screen.getByText(/Ставка дисконтирования отсутствует или неоднозначна/)).toBeTruthy();
});

test('valuation sensitivity escapes all source text and keeps unsupported empty states explicit',()=>{
  const attack='<img src=x onerror="alert(1)">';const data=sensitivityFixture();
  data.scenarios[0].raw_unit=attack;data.scenarios[0].explicit_formula=attack;data.scenarios[0].source_inputs[0].formula=attack;
  data.scenarios[0].target.sheet=attack;data.limitations=[attack];
  const {container,rerender}=render(<ValuationSensitivityPanel data={data} onEvidence={()=>{}}/>);
  expect(container.querySelector('img')).toBeNull();expect(container.textContent).toContain(attack);
  rerender(<ValuationSensitivityPanel data={{status:'unsupported',scenarios:[],checks:[],limitations:['Нет подходящей формулы.']}} onEvidence={()=>{}}/>);
  expect(screen.getByText('Нет поддержанных сценариев')).toBeTruthy();
  expect(screen.getByText('Поддержанные сценарии не представлены.')).toBeTruthy();
  expect(screen.getByText('Нет подходящей формулы.')).toBeTruthy();
  rerender(<ValuationSensitivityPanel data={null} onEvidence={()=>{}}/>);expect(container.textContent).toBe('');
});

test('typed comparison exposes exact periods formula and clickable saved inputs',()=>{
  const onEvidence=vi.fn();const comparison={kind:'cagr',formula:'(end / start) ** (1 / years) - 1',
    periods:{start:2019,end:2031,years:12},source_unit:'RUB M',basis:'saved_values',recalculated:false,
    inputs:[{role:'start',sheet:'Financials',cell:'K18',value:100,period:2019},{role:'end',sheet:'Financials',cell:'W18',value:400,period:2031}]};
  render(<ReportView report={{stages:[{id:'audit',title:'Проверка',summary:'Источник',observations:[{title:'CAGR',detail:'Среднегодовой рост',comparison,status:'observation',impact:'not_assessed',evidence:[]}]}]}} onEvidence={onEvidence}/>);
  const summary=screen.getByText('Источники и формула сравнения');fireEvent.click(summary);
  expect(screen.getByText(/Периоды: 2019 → 2031/).textContent).toContain('12 лет');
  expect(screen.getByText(comparison.formula)).toBeTruthy();
  expect(screen.getByText(/Начало · 2019 · сохранённое значение: 100/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'Financials!W18'}));
  expect(onEvidence).toHaveBeenCalledWith({sheet:'Financials',cell:'W18',role:'end',value:400,period:2031});
});

test('typed comparison keeps raw labels literal and handles missing data',()=>{
  const attack='<img src=x onerror="alert(1)">';
  const {container,rerender}=render(<ComparisonDetails data={{formula:attack,source_unit:attack,periods:{},inputs:[{role:'start',value:0,sheet:attack,cell:'A1'}]}} onEvidence={()=>{}}/>);
  expect(container.querySelector('img')).toBeNull();
  expect(screen.getAllByText(attack).length).toBeGreaterThan(0);
  expect(container.textContent).toContain('сохранённое значение: 0');
  rerender(<ComparisonDetails data={null} onEvidence={()=>{}}/>);
  expect(container.textContent).toBe('');
});
test('zero and missing saved value are distinct',()=>{
  expect(displayValue(0)).toBe('0');
  expect(displayValue(null)).toContain('Нет');
});
test('rejects an unsupported file before a request',()=>{
  global.fetch=vi.fn(); render(<UploadPanel configured onCreated={()=>{}}/>);
  fireEvent.change(screen.getByLabelText('Финансовая модель Excel'),{target:{files:[new File(['x'],'data.csv')]}});
  expect(screen.getByRole('alert').textContent).toContain('.xlsx');
  expect(fetch).not.toHaveBeenCalled();
});
test('upload sends chosen file and mode and returns server job',async()=>{
  const created=vi.fn();const job={id:'job-1',filename:'model.xlsx',status:'queued'};
  global.fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>job});
  render(<UploadPanel configured onCreated={created}/>);
  const file=new File(['xlsx'],'model.xlsx');
  fireEvent.change(screen.getByLabelText('Финансовая модель Excel'),{target:{files:[file]}});
  fireEvent.click(screen.getByRole('radio',{name:/Локальная проверка/}));
  fireEvent.click(screen.getByRole('button',{name:/Запустить анализ/}));
  await waitFor(()=>expect(created).toHaveBeenCalledWith(job));
  expect(fetch.mock.calls[0][1].body.get('mode')).toBe('local');
  expect(fetch.mock.calls[0][1].body.get('file').name).toBe('model.xlsx');
});
test('server error is visible without claiming success',async()=>{
  global.fetch=vi.fn().mockResolvedValue({ok:false,json:async()=>({detail:'Книга повреждена'})});
  const created=vi.fn();render(<UploadPanel configured onCreated={created}/>);
  fireEvent.change(screen.getByLabelText('Финансовая модель Excel'),{target:{files:[new File(['x'],'bad.xlsx')]}});
  fireEvent.click(screen.getByRole('button',{name:/Запустить анализ/}));
  await waitFor(()=>expect(screen.getByRole('alert').textContent).toContain('повреждена'));
  expect(created).not.toHaveBeenCalled();
});
test('invalid replacement clears a previously selected workbook',()=>{
  global.fetch=vi.fn();render(<UploadPanel configured onCreated={()=>{}}/>);
  const input=screen.getByLabelText('Финансовая модель Excel');
  fireEvent.change(input,{target:{files:[new File(['x'],'valid.xlsx')]}});
  fireEvent.change(input,{target:{files:[new File(['x'],'invalid.csv')]}});
  fireEvent.click(screen.getByRole('button',{name:/Запустить анализ/}));
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.getByRole('alert').textContent).toContain('Сначала выберите');
});
test('sensitivity table distinguishes a zero result from a failed scenario',()=>{
  render(<CalculationPanel onEvidence={()=>{}} items={[{result:{
    expression:'x-10',baseline:{input_value:10,result:{value:0}},
    scenarios:[{change_fraction:0,input_value:10,result:{value:0},absolute_output_change:0},
               {change_fraction:.1,input_value:11,result:{error:'failed'},absolute_output_change:null}],
    evidence:[{name:'x',sheet:'DCF',cell:'E10',value:10}],
  }}]}/>);
  expect(screen.getByText('Не рассчитано')).toBeTruthy();
  expect(screen.getAllByText('0').length).toBe(3);
  expect(screen.getByRole('button',{name:'DCF!E10',hidden:true})).toBeTruthy();
});
test('incomplete stages remain visible in a generated report',()=>{
  render(<ReportView report={{stages:[{id:'assumptions',completion:'incomplete',status:'limited'}]}} onEvidence={()=>{}}/>);
  expect(screen.getByRole('status').textContent).toContain('Частичный отчёт');
  expect(screen.getByRole('status').textContent).toContain('Предпосылки');
});
test('job polling recovers after a transient network failure',async()=>{
  const job={id:'retry-job',filename:'retry.xlsx',status:'running',mode:'local'};
  let detailCalls=0;
  global.fetch=vi.fn(async(url)=>{
    let data;
    if(url==='/api/health')data={provider:{configured:true}};
    else if(url==='/api/jobs')data={jobs:[job]};
    else if(url==='/api/jobs/retry-job'){
      if(++detailCalls===1)throw new Error('Временный сбой соединения');
      data={...job,status:'completed',report_available:true};
    }else if(url==='/api/jobs/retry-job/report')data={stages:[{id:'audit',status:'limited',completion:'submitted',summary:'Восстановленный отчёт'}]};
    else throw new Error('Unexpected test request');
    return{ok:true,json:async()=>data};
  });
  render(<App/>);
  fireEvent.click(await screen.findByRole('button',{name:/retry.xlsx/}));
  await screen.findByText('Временный сбой соединения');
  await waitFor(()=>expect(screen.getByText('Восстановленный отчёт')).toBeTruthy(),{timeout:4000});
  expect(detailCalls).toBe(2);
});

const sourceFact = {id:'posi-profit-2029',stage_id:'business',source_sheet:'FSs - POSI',
  value_cell:'U37',value:1234.5,formula:'=U18-U30',metric_label:'Operating profit',metric_cell:'E37',metric_key:'operating_profit',
  period:2029,period_cell:'U8',unit:'млн руб.',unit_cell:'E9',period_type:'forecast',period_type_cell:'U7',binding_status:'bound',issues:[]};

test('stage fact table shows exact source labels and opens each source reference',()=>{
  const select=vi.fn();
  render(<ReportView report={{stages:[{id:'audit',title:'Аудит'},{id:'business',title:'Бизнес'}],facts:[sourceFact,{...sourceFact,id:'orphan',stage_id:undefined,metric_label:'Без стадии'}]}} onEvidence={select}/>);
  expect(screen.queryByRole('region',{name:'Данные из модели'})).toBeNull();
  fireEvent.click(screen.getByRole('tab',{name:/Обзор бизнеса/}));
  const panel=within(screen.getByRole('region',{name:'Данные из модели'}));
  expect(panel.getByText('Operating profit')).toBeTruthy();
  expect(panel.getByText('2029')).toBeTruthy();
  expect(panel.getByText('Прогноз')).toBeTruthy();
  expect(panel.getByText('Лист: FSs - POSI')).toBeTruthy();
  expect(screen.queryByText('Без стадии')).toBeNull();
  expect(panel.getByRole('columnheader',{name:'Сохранённое значение'})).toBeTruthy();
  expect(panel.getByRole('columnheader',{name:'Подпись единицы'})).toBeTruthy();
  for(const [label,cell] of [['Сохранённое значение','U37'],['Показатель','E37'],['Период','U8'],['Подпись единицы','E9'],['Тип периода','U7']]){
    fireEvent.click(panel.getByRole('button',{name:new RegExp(`^${label}:`)}));
    expect(select).toHaveBeenLastCalledWith({sheet:'FSs - POSI',cell});
  }
  expect(panel.queryByText('EBITDA')).toBeNull();
});

test('fact cards preserve missing context and render untrusted source text as text',()=>{
  const attack='<img src=x onerror=alert(1)>';
  const {container}=render(<FactPanel onEvidence={()=>{}} items={[
    {...sourceFact,value:0,metric_label:attack,formula:'=<script>alert(1)</script>',period:null,period_cell:null,unit:null,unit_cell:null,period_type:null,period_type_cell:null,binding_status:'ambiguous',issues:[{code:'conflict',message:attack,source_cells:['U8','U9']}]},
    {...sourceFact,id:'missing',value:null,binding_status:'unsupported',issues:[]},
  ]}/>);
  expect(screen.getByText('0')).toBeTruthy();
  expect(screen.getByText('Нет сохранённого значения')).toBeTruthy();
  expect(screen.getByText('Период не определён')).toBeTruthy();
  expect(screen.getByText('Единица не определена')).toBeTruthy();
  expect(screen.getByText('Факт/прогноз не определён')).toBeTruthy();
  expect(screen.getByText('Неоднозначная связь')).toBeTruthy();
  expect(screen.getByText('Значение не поддерживается')).toBeTruthy();
  expect(screen.getAllByText(attack)).toHaveLength(2);
  expect(container.querySelector('img,script')).toBeNull();
  expect(screen.getByRole('button',{name:'FSs - POSI!U9'})).toBeTruthy();
});

test('legacy report without fact cards keeps its observations and no empty fact section',()=>{
  render(<ReportView report={{stages:[{id:'audit',title:'Аудит',summary:'Ранее сохранённый отчёт',observations:[{title:'Замечание',detail:'Сохранённая ошибка',evidence:[]}]}]}} onEvidence={()=>{}}/>);
  expect(screen.getByText('Ранее сохранённый отчёт')).toBeTruthy();
  expect(screen.getByText('Сохранённая ошибка')).toBeTruthy();
  expect(screen.queryByRole('region',{name:'Данные из модели'})).toBeNull();
  expect(screen.queryByRole('region',{name:'Финансовые проверки'})).toBeNull();
  expect(screen.queryByRole('region',{name:'Дополнительные проверки Excel'})).toBeNull();
});

test('value and period fact links load their actual cells into the existing drawer',async()=>{
  const job={id:'fact-job',filename:'facts.xlsx',status:'completed',mode:'gigachat',report_available:true};
  global.fetch=vi.fn(async(url)=>{
    let data;
    if(url==='/api/health')data={provider:{configured:true}};
    else if(url==='/api/jobs')data={jobs:[job]};
    else if(url==='/api/jobs/fact-job')data=job;
    else if(url==='/api/jobs/fact-job/report')data={stages:[{id:'audit',title:'Аудит'}],facts:[{...sourceFact,stage_id:'audit'}]};
    else if(url.startsWith('/api/jobs/fact-job/cells?')){
      const query=new URLSearchParams(url.split('?')[1]);
      expect(query.get('sheet')).toBe('FSs - POSI');
      const cell=query.get('range');
      data={cells:[{cell,value:cell==='U8'?2029:1234.5,formula:cell==='U37'?'=U18-U30':null}]};
    }else throw new Error('Unexpected test request');
    return{ok:true,json:async()=>data};
  });
  render(<App/>);
  fireEvent.click(await screen.findByRole('button',{name:/facts.xlsx/}));
  fireEvent.click(await screen.findByRole('button',{name:/^Сохранённое значение:/}));
  let drawer=within(await screen.findByRole('dialog',{name:'Источник в Excel'}));
  await waitFor(()=>expect(drawer.getByText('=U18-U30')).toBeTruthy());
  expect(drawer.getByText('U37')).toBeTruthy();
  fireEvent.click(drawer.getByRole('button',{name:'Закрыть источник'}));
  fireEvent.click(screen.getByRole('button',{name:/^Период:/}));
  drawer=within(await screen.findByRole('dialog',{name:'Источник в Excel'}));
  await waitFor(()=>expect(drawer.getByText(displayValue(2029).replace(/\s/g,' '))).toBeTruthy());
  expect(drawer.getByText('U8')).toBeTruthy();
});

const financialChecks = {version:'financial-1',status:'limited',summary:{candidate_count:1,observation_count:0,checked_count:2,unsupported_count:1},
  findings:[{id:'repeated-flow',code:'repeat',status:'candidate',title:'Возможное повторное включение потока',detail:'Два слагаемых требуют проверки.',financial_materiality:'not_assessed',
    evidence:[{sheet:'DCF',cell:'E39'},{sheet:'WACC',cell:'E13'}],
    calculation:{label:'Сравнение сохранённых значений',expression:'left-right',value:0,actual:100,expected:100,difference:0,unit:'млн руб.',basis:'saved_values',recalculated:false,inputs:[{sheet:'DCF',cell:'E39',value:100}],limitations:['Экономическая эквивалентность не установлена.']}}],
  checks:[{code:'other',status:'unsupported',detail:'Не удалось разобрать сложное выражение.',evidence:[{sheet:'DCF',cell:'E40'}]}],
  limitations:['Свежий пересчёт Excel не выполнялся.']};

test('a small nonzero check difference is not rounded to zero',()=>{
  render(<CheckPanel title="Финансовые проверки" data={{findings:[{status:'candidate',calculation:{value:0,difference:1e-8}}]}} onEvidence={()=>{}}/>);
  expect(screen.getByText('0')).toBeTruthy();
  expect(screen.getByText('1e-8')).toBeTruthy();
});

test('deterministic findings stay candidates with explicit scope and exact sources',()=>{
  const select=vi.fn();
  render(<ReportView report={{financial_checks:financialChecks}} onEvidence={select}/>);
  const panel=within(screen.getByRole('region',{name:'Финансовые проверки'}));
  expect(panel.getByText('Выполнены с ограничениями')).toBeTruthy();
  expect(panel.getByText('Требует проверки')).toBeTruthy();
  expect(panel.getByText('Наблюдений: 0')).toBeTruthy();
  expect(panel.getByText('Финансовая существенность замечаний не оценена.')).toBeTruthy();
  fireEvent.click(panel.getByRole('button',{name:'WACC!E13'}));
  expect(select).toHaveBeenLastCalledWith({sheet:'WACC',cell:'E13'});
  fireEvent.click(panel.getByText('Сравнение сохранённых значений'));
  expect(panel.getAllByText('0')).toHaveLength(2);
  expect(panel.getByText('Сравниваемое значение')).toBeTruthy();
  expect(panel.getByText('Экономическая эквивалентность не установлена.')).toBeTruthy();
  fireEvent.click(panel.getByText('Охват проверок · 1'));
  expect(panel.getByText('Не поддерживается')).toBeTruthy();
  expect(panel.getByText('Свежий пересчёт Excel не выполнялся.')).toBeTruthy();
});

test('supplemental unavailable and partial results cannot look like a clean bill',()=>{
  const {rerender}=render(<CheckPanel title="Дополнительные проверки Excel" data={{status:'unavailable',limitations:['Пакет проверки недоступен.']}} onEvidence={()=>{}}/>);
  expect(screen.getByText('Недоступны')).toBeTruthy();
  expect(screen.getByText('Пакет проверки недоступен.')).toBeTruthy();
  expect(screen.queryByText('Проверки выполнены')).toBeNull();
  rerender(<CheckPanel title="Дополнительные проверки Excel" data={{status:'partial',findings:[],summary:{total:42,retained:0,suppressed:40,truncated_count:2,truncated:true,upstream_complete:false},checks:null,limitations:[]}} onEvidence={()=>{}}/>);
  expect(screen.getByText('Выполнены частично')).toBeTruthy();
  expect(screen.getByText('Включено в отчёт: 0')).toBeTruthy();
  expect(screen.getByText('Не вошло в лимит: 2')).toBeTruthy();
  expect(screen.getByText('Исходная проверка выполнена не полностью.')).toBeTruthy();
  expect(screen.getByText('Замечания не представлены.')).toBeTruthy();
});

test('check findings and calculation text are escaped and metadata is not a financial verdict',()=>{
  const attack='<img src=x onerror=alert(1)>';
  const {container}=render(<CheckPanel title="Дополнительные проверки Excel" data={{status:'complete',summary:{total:1},limitations:[attack],findings:[{status:'candidate',title:attack,detail:attack,evidence:[{sheet:attack,cell:'E39'}],origin_metadata:{severity:'critical',api_key:'internal-only'},confidence:'high',calculation:{label:attack,expression:attack,unit:attack,internal_trace:'internal-only',inputs:[{sheet:attack,cell:'E39',value:0}]}}]}} onEvidence={()=>{}}/>);
  expect(screen.getByText('Проверки выполнены')).toBeTruthy();
  expect(screen.getByText('Требует проверки')).toBeTruthy();
  expect(container.querySelector('img,script')).toBeNull();
  expect(container.textContent).toContain(attack);
  expect(container.textContent).not.toContain('critical');
  expect(container.textContent).not.toContain('internal-only');
});

test('financial finding opens the original cell drawer with the correct sheet',async()=>{
  const job={id:'checks-job',filename:'checks.xlsx',status:'completed',mode:'local',report_available:true};
  global.fetch=vi.fn(async(url)=>{
    let data;
    if(url==='/api/health')data={provider:{configured:true}};
    else if(url==='/api/jobs')data={jobs:[job]};
    else if(url==='/api/jobs/checks-job')data=job;
    else if(url==='/api/jobs/checks-job/report')data={financial_checks:financialChecks};
    else if(url.startsWith('/api/jobs/checks-job/cells?')){
      const query=new URLSearchParams(url.split('?')[1]);
      expect(query.get('sheet')).toBe('WACC');expect(query.get('range')).toBe('E13');
      data={cells:[{cell:'E13',value:.087,formula:'=E10+E11'}]};
    }else throw new Error('Unexpected test request');
    return {ok:true,json:async()=>data};
  });
  render(<App/>);
  fireEvent.click(await screen.findByRole('button',{name:/checks.xlsx/}));
  fireEvent.click(await screen.findByRole('button',{name:'WACC!E13'}));
  const drawer=within(await screen.findByRole('dialog',{name:'Источник в Excel'}));
  await waitFor(()=>expect(drawer.getByText('=E10+E11')).toBeTruthy());
  expect(drawer.getByText('WACC')).toBeTruthy();
});
