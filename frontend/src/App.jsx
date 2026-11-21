import React, {useCallback, useEffect, useRef, useState} from 'react';
import {api, STAGES, STATUS, displayValue} from './api.js';
import Evidence from './SourceLinks.jsx';
import {ReportOverview, KeyMetrics, FinancialGroups, ComparisonCharts, SensitivityCharts, isShownFactDuplicate} from './PresentationView.jsx';
import ImpactPanel from './ImpactPanel.jsx';
import DecisionPanel from './DecisionPanel.jsx';
import PptxDownload from './PptxDownload.jsx';

function Icon({name, ...props}) {
  const paths = {file: 'M8 3h7l5 5v13H4V3h4m7 0v6h5M8 13h8M8 17h5',
    upload: 'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
    arrow: 'M5 12h14m-6-6 6 6-6 6', close: 'M6 6l12 12M6 18 18 6',
    check: 'm5 12 4 4L19 6', search: 'm16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0'};
  return <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}><path d={paths[name] || paths.file}/></svg>;
}

export function UploadPanel({configured, onCreated}) {
  const [file, setFile] = useState(null), [mode, setMode] = useState('gigachat');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const input = useRef(null);
  const select = f => {
    setError('');
    if (!f) return;
    setFile(null);
    if (!f.name.toLowerCase().endsWith('.xlsx')) {setError('Выберите книгу в формате .xlsx.'); return;}
    if (f.size > 20 * 1024 * 1024) {setError('Размер книги должен быть не больше 20 МБ.'); return;}
    setFile(f);
  };
  async function submit(event) {
    event.preventDefault();
    if (!file) {setError('Сначала выберите Excel-файл.'); return;}
    setBusy(true); setError('');
    try {
      const body = new FormData(); body.append('file', file); body.append('mode', mode);
      const job = await api('/jobs', {method: 'POST', body}); onCreated(job);
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  return <section className="upload-section"><div className="eyebrow">НОВЫЙ АНАЛИЗ</div>
    <h2>Разобраться в модели.<br/>Видеть источники выводов.</h2>
    <p className="intro">Загрузите финансовую модель. Сервис проверит структуру расчётов и подготовит четыре раздела аналитического отчёта.</p>
    <form onSubmit={submit}>
      <div className="dropzone" onDragOver={e => e.preventDefault()} onDrop={e => {e.preventDefault();select(e.dataTransfer.files[0]);}}>
        <div className="upload-icon"><Icon name={file ? 'file' : 'upload'}/></div>
        <strong>{file ? file.name : 'Перетащите Excel-файл сюда'}</strong>
        <span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} МБ · оригинал сохраняется` : 'XLSX, до 20 МБ'}</span>
        <button type="button" className="secondary" onClick={() => input.current.click()}>{file ? 'Выбрать другую книгу' : 'Выбрать файл'}</button>
        <input ref={input} type="file" accept=".xlsx" aria-label="Финансовая модель Excel" onChange={e => select(e.target.files[0])} hidden/>
      </div>
      <fieldset className="mode-select"><legend>Режим анализа</legend>
        <label><input type="radio" name="mode" value="gigachat" checked={mode === 'gigachat'} onChange={() => setMode('gigachat')}/><span><strong>Анализ с GigaChat</strong><small>Проверка расчётов и интерпретация модели</small></span></label>
        <label><input type="radio" name="mode" value="local" checked={mode === 'local'} onChange={() => setMode('local')}/><span><strong>Локальная проверка</strong><small>Проверки формул и финансовых связей, без LLM</small></span></label>
      </fieldset>
      {mode === 'gigachat' && <p className="data-note">Выбранные агентом фрагменты книги будут переданы в API GigaChat.</p>}
      {mode === 'gigachat' && configured === false && <p className="notice">GigaChat пока не настроен. Доступна локальная проверка.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary" disabled={busy || (mode === 'gigachat' && configured === false)}>{busy ? 'Загружаем книгу…' : 'Запустить анализ'}<Icon name="arrow"/></button>
    </form>
  </section>;
}

const FACT_BINDING = {bound: 'Контекст связан', partial: 'Контекст неполный', ambiguous: 'Неоднозначная связь', unknown: 'Связь не установлена', unsupported: 'Значение не поддерживается'};
const PERIOD_TYPE = {actual: 'Факт', forecast: 'Прогноз', unknown: 'Факт/прогноз не определён', ambiguous: 'Тип периода неоднозначен'};

function FactSource({fact, field, cellField, label, fallback, onEvidence, numeric = false}) {
  const value = fact[field];
  const text = value == null || value === '' ? fallback : numeric ? displayValue(value) : String(value);
  const address = fact[cellField];
  if (typeof fact.source_sheet !== 'string' || !fact.source_sheet || typeof address !== 'string' || !address) return <span className="fact-missing">{text}</span>;
  return <button type="button" className="fact-source" title={numeric && value!=null ? String(value) : undefined} aria-label={`${label}: ${text}. Источник ${fact.source_sheet}!${address}`}
    onClick={() => onEvidence({sheet: fact.source_sheet, cell: address})}>
    <span>{text}</span><small>{address}</small>
  </button>;
}

export function FactPanel({items, onEvidence}) {
  if (!items?.length) return null;
  return <section className="fact-panel" aria-label="Данные из модели">
    <h3>Данные из модели <span>· {items.length}</span></h3>
    <p className="fact-note">Связь с источниками не подтверждает финансовый вывод. Название листа указывает источник, а не периметр компании.</p>
    <div className="table-scroll"><table className="fact-table"><thead><tr><th scope="col">Показатель</th><th scope="col">Сохранённое значение</th><th scope="col">Период</th><th scope="col">Подпись единицы</th><th scope="col">Связь с источниками</th></tr></thead><tbody>
      {items.map((fact, index) => <tr key={`${fact.id || 'fact'}-${index}`}>
        <td><FactSource fact={fact} field="metric_label" cellField="metric_cell" label="Показатель" fallback="Подпись не определена" onEvidence={onEvidence}/><small className="fact-sheet">Лист: {fact.source_sheet || 'не указан'}</small>
          {fact.formula && <details className="fact-formula"><summary>Формула</summary><code>{String(fact.formula)}</code></details>}</td>
        <td><FactSource fact={fact} field="value" cellField="value_cell" label="Сохранённое значение" fallback="Нет сохранённого значения" numeric onEvidence={onEvidence}/></td>
        <td><FactSource fact={fact} field="period" cellField="period_cell" label="Период" fallback="Период не определён" onEvidence={onEvidence}/>
          <div className="fact-period-type"><FactSource fact={{...fact, period_type_label: PERIOD_TYPE[fact.period_type] || PERIOD_TYPE.unknown}} field="period_type_label" cellField="period_type_cell" label="Тип периода" fallback={PERIOD_TYPE.unknown} onEvidence={onEvidence}/></div></td>
        <td><FactSource fact={fact} field="unit" cellField="unit_cell" label="Подпись единицы" fallback="Единица не определена" onEvidence={onEvidence}/></td>
        <td><span className={`fact-binding ${fact.binding_status === 'bound' ? 'bound' : 'uncertain'}`}>{FACT_BINDING[fact.binding_status] || FACT_BINDING.unknown}</span>
          {Array.isArray(fact.issues) && fact.issues.length > 0 && <ul className="fact-issues">{fact.issues.map((issue,i)=><li key={i}>{typeof issue === 'object' && issue ? String(issue.message || issue.code || 'Контекст требует проверки') : String(issue)}
            {Array.isArray(issue?.source_cells) && <Evidence items={issue.source_cells.filter(cell=>typeof cell === 'string').map(cell=>({sheet:fact.source_sheet,cell}))} onSelect={onEvidence}/>}</li>)}</ul>}</td>
      </tr>)}
    </tbody></table></div>
  </section>;
}

export function CalculationPanel({items, onEvidence}) {
  if (!items?.length) return null;
  return <details className="technical calculations"><summary>Дополнительные расчёты · {items.length}</summary>
    <p>Сценарии меняют только выбранное выражение при фиксированных остальных входных данных. Полный пересчёт книги не выполняется.</p>
    {items.map((item, index) => {
      const data=item.result || {};
      const value=data.result?.value;
      return <article key={index}><h3>{data.scenarios ? 'Локальная чувствительность' : 'Проверочный расчёт'}</h3>
        <pre>{data.expression || item.arguments?.expression}</pre>
        {data.scenarios ? <div className="table-scroll"><table><thead><tr><th>Изменение входа</th><th>Значение входа</th><th>Результат</th><th>Изменение результата</th></tr></thead><tbody>
          <tr><th>Базовое значение</th><td>{displayValue(data.baseline?.input_value)}</td><td>{data.baseline?.result?.value == null ? 'Не рассчитано' : displayValue(data.baseline.result.value)}</td><td>—</td></tr>
          {data.scenarios.map((row,i)=><tr key={i}><th>{displayValue(row.change_fraction*100)}%</th><td>{displayValue(row.input_value)}</td><td>{row.result?.value == null ? 'Не рассчитано' : displayValue(row.result.value)}</td><td>{row.absolute_output_change == null ? '—' : displayValue(row.absolute_output_change)}</td></tr>)}
        </tbody></table></div> : <p>Результат: <strong>{value == null ? 'Не рассчитано' : displayValue(value)}</strong></p>}
        <ul className="calculation-inputs">{data.evidence?.map((input,i)=><li key={i}>{input.name} = {displayValue(input.value)} · {input.sheet}!{input.cell}</li>)}</ul>
        <Evidence items={data.evidence} onSelect={onEvidence}/>
      </article>;
    })}
  </details>;
}

const CHECK_STATUS = {complete:'Проверки выполнены', completed:'Проверки выполнены', partial:'Выполнены частично', limited:'Выполнены с ограничениями', unavailable:'Недоступны', not_applicable:'Не применимы', timeout:'Прерваны по времени', failed:'Не удалось выполнить', skipped:'Не выполнялись'};
const CHECK_ITEM_STATUS = {checked:'Выполнена', completed:'Выполнена', not_applicable:'Не применима', unsupported:'Не поддерживается', ambiguous:'Неоднозначный контекст'};
const CHECK_COUNTS = {candidate_count:'Требуют проверки', observation_count:'Наблюдений', checked_count:'Проверок выполнено', unsupported_count:'Не поддерживается', ambiguous_count:'Неоднозначный контекст', total:'Всего замечаний', eligible:'После отбора', retained:'Включено в отчёт', suppressed:'Отсеяно', truncated_count:'Не вошло в лимит'};
const checkObject = value => value && typeof value === 'object' && !Array.isArray(value);
const checkText = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
const checkEvidence = value => Array.isArray(value) ? value.filter(ref=>checkObject(ref) && typeof ref.sheet==='string' && typeof ref.cell==='string') : [];
const checkLimits = value => Array.isArray(value) ? value.filter(item=>typeof item==='string') : [];

function CheckCalculation({data, onEvidence}) {
  if (!checkObject(data)) return null;
  const values = [['value','Результат выражения'],['actual','В модели'],['expected','Сравниваемое значение'],['difference','Разница']].filter(([key])=>typeof data[key]==='number' && Number.isFinite(data[key]));
  const inputs = Array.isArray(data.inputs) ? data.inputs.filter(checkObject) : [];
  const limits = checkLimits(data.limitations);
  return <details className="check-calculation"><summary>{checkText(data.label) || 'Расчёт к замечанию'}</summary>
    {data.basis==='saved_values' && <p>На основе сохранённых значений.</p>}
    {checkText(data.expression) && <pre>{checkText(data.expression)}</pre>}
    {values.length > 0 && <dl className="check-values">{values.map(([key,label])=><div key={key}><dt>{label}</dt><dd>{String(data[key])}</dd></div>)}</dl>}
    {checkText(data.unit) && <p>Подпись единицы: {checkText(data.unit)}</p>}
    {inputs.length > 0 && <ul className="check-inputs">{inputs.map((item,i)=><li key={i}><span>{item.value==null?'Нет сохранённого значения':checkText(item.value)}</span><Evidence items={checkEvidence([item])} onSelect={onEvidence}/></li>)}</ul>}
    {data.recalculated===false && <p>Полный пересчёт книги не выполнялся.</p>}
    {limits.length > 0 && <ul>{limits.map((text,i)=><li key={i}>{text}</li>)}</ul>}
  </details>;
}

export function ComparisonDetails({data, onEvidence}) {
  if (!checkObject(data)) return null;
  const periods = checkObject(data.periods) ? data.periods : {};
  const inputs = Array.isArray(data.inputs) ? data.inputs.filter(checkObject) : [];
  return <details className="check-calculation comparison-details">
    <summary>Источники и формула сравнения</summary>
    <p>Периоды: {checkText(periods.start) || 'не установлен'} → {checkText(periods.end) || 'не установлен'}{Number.isInteger(periods.years) && periods.years>0 ? ` · интервал: ${periods.years} лет` : ''}.</p>
    {checkText(data.formula) && <pre>{checkText(data.formula)}</pre>}
    {checkText(data.source_unit) && <p>Исходная подпись единицы: {checkText(data.source_unit)}</p>}
    {inputs.length>0 && <ul className="check-inputs">{inputs.map((item,index)=><li key={index}>
      <span>{{start:'Начало',end:'Конец'}[item.role] || 'Вход'} · {checkText(item.period) || 'период не установлен'} · сохранённое значение: {item.value==null?'не установлено':checkText(item.value)}</span>
      <Evidence items={checkEvidence([item])} onSelect={onEvidence}/>
    </li>)}</ul>}
    {data.basis==='saved_values' && <p>Расчёт по сохранённым значениям выбранных источников.</p>}
    {data.recalculated===false && <p>Полный пересчёт книги не выполнялся.</p>}
  </details>;
}

const SENSITIVITY_PARAMETERS = {discount_rate:'Ставка дисконтирования',terminal_growth:'Терминальный рост'};
const SENSITIVITY_REASONS = {
  alias_of_recognized_output:'Отдельный сценарий не строился: ссылка на уже рассмотренный выход.',
  original_formula_arithmetic_only:'Поддержана локальная арифметика исходной формулы.',
  rate_only_no_supported_terminal_growth_branch:'Поддержано изменение ставки; подходящая ветвь терминального роста не найдена.',
  ambiguous_or_missing_discount_rate:'Ставка дисконтирования отсутствует или неоднозначна.',
  ambiguous_terminal_growth:'Терминальный рост неоднозначен.',
  baseline_does_not_match_saved_output:'Расчёт при исходных входах не совпадает с сохранённым выходом.',
  missing_or_ambiguous_output_unit:'Подпись единицы выхода отсутствует или неоднозначна.',
  scenario_rate_not_above_growth:'В сценарии ставка дисконтирования не выше терминального роста.',
  invalid_effective_parameters:'Исходные эффективные параметры не позволяют построить сценарий.',
  unresolved_dependency_in_output_sources:'Не все зависимости выхода разрешены.',
  discount_rate_not_explicit_cell:'Не определена отдельная ячейка ставки дисконтирования.',
  terminal_growth_not_explicit_cell:'Не определена отдельная ячейка терминального роста.',
  terminal_growth_label_not_established:'Подпись терминального роста не подтверждена.',
};
const finiteNumber = value => typeof value==='number' && Number.isFinite(value);
const sensitivityChange = data => data?.change_unit==='absolute_fraction' && finiteNumber(data.change)
  ? `${data.change>0?'+':''}${Number((data.change*100).toPrecision(12))} п.п.` : 'изменение не установлено';

export function ValuationSensitivityPanel({data, onEvidence}) {
  if (!checkObject(data)) return null;
  const scenarios=Array.isArray(data.scenarios)?data.scenarios.filter(checkObject):[];
  const checks=Array.isArray(data.checks)?data.checks.filter(checkObject):[];
  const limits=checkLimits(data.limitations);
  return <section className="check-panel valuation-panel" aria-label="Чувствительность оценки">
    <div className="section-heading"><h2>Чувствительность оценки</h2><span className="check-run-status">{data.status==='limited'?'Рассчитана с ограничениями':data.status==='unsupported'?'Нет поддержанных сценариев':'Статус не указан'}</span></div>
    <p className="check-note">Сценарии исходной формулы при фиксированных незатронутых входах и FCFF. Полный пересчёт Excel не выполнялся.</p>
    <p className="check-note">Сценариев: {scenarios.length}</p>
    {scenarios.map((item,index)=>{
      const parameter=checkObject(item.changed_parameter)?item.changed_parameter:{};
      const formulas=Array.isArray(item.formula_path)?item.formula_path.filter(checkObject):[];
      const inputs=Array.isArray(item.source_inputs)?item.source_inputs.filter(checkObject):[];
      const periods=Array.isArray(item.periods)?item.periods.filter(checkObject):[];
      const effective=Array.isArray(item.effective_parameters)?item.effective_parameters.filter(checkObject):[];
      const frozen=checkEvidence(item.frozen_cashflows);
      return <article className="check-finding sensitivity-scenario" key={index} aria-label={`Сценарий ${index+1}`}>
        <h3>{SENSITIVITY_PARAMETERS[item.parameter] || 'Параметр'} · {sensitivityChange(parameter)}</h3>
        <div className="sensitivity-source"><span>Целевой выход</span><Evidence items={checkEvidence([item.target])} onSelect={onEvidence}/></div>
        <div className="sensitivity-source"><span>Изменяемый параметр</span><Evidence items={checkEvidence([parameter])} onSelect={onEvidence}/></div>
        <p>Параметр (доля): {finiteNumber(parameter.baseline)?String(parameter.baseline):'не установлен'} → {finiteNumber(parameter.effective_value)?String(parameter.effective_value):'не установлен'}.</p>
        <dl className="check-values">{[['saved_output','Сохранённый выход'],['baseline','Выход при исходных входах'],['result','Выход сценария'],['delta','Изменение выхода']].filter(([key])=>finiteNumber(item[key])).map(([key,label])=><div key={key}><dt>{label}</dt><dd>{String(item[key])}</dd></div>)}</dl>
        {checkText(item.raw_unit) && <p>Подпись единицы выхода: {checkText(item.raw_unit)}</p>}
        {finiteNumber(item.relative_delta) && <p>Относительное изменение выхода: {Number((item.relative_delta*100).toPrecision(10))}%.</p>}
        <details className="check-calculation"><summary>Формулы, входы и периоды сценария</summary>
          {checkText(item.explicit_formula) && <pre>{checkText(item.explicit_formula)}</pre>}
          {item.basis==='saved_unaffected_inputs_and_original_formula' && <p>Исходная формула сохранена; незатронутые входы зафиксированы на сохранённом уровне.</p>}
          {finiteNumber(item.baseline_cache_difference) && <p>Отличие исходного расчёта от сохранённого выхода: {String(item.baseline_cache_difference)}.</p>}
          {effective.length>0 && <><h4>Эффективные параметры (доли)</h4><ul className="check-inputs">{effective.map((value,i)=><li key={i}><span>{SENSITIVITY_PARAMETERS[value.role] || 'Параметр'}: {checkText(value.value)}</span><Evidence items={checkEvidence([value])} onSelect={onEvidence}/></li>)}</ul></>}
          {frozen.length>0 && <><h4>FCFF зафиксированы на сохранённом уровне</h4><Evidence items={frozen} onSelect={onEvidence}/></>}
          {formulas.length>0 && <><h4>Формулы пути к выходу</h4>{formulas.map((value,i)=><div className="sensitivity-formula" key={i}><Evidence items={checkEvidence([value])} onSelect={onEvidence}/><pre>{checkText(value.formula)}</pre></div>)}</>}
          {inputs.length>0 && <><h4>Исходные входы</h4>{inputs.map((value,i)=><div className="sensitivity-input" key={i}><p>Сохранённое значение: {value.value==null?'не установлено':checkText(value.value)}{value.frozen===true?' · зафиксировано':value.frozen===false?' · изменяемый вход':''}</p><Evidence items={checkEvidence([value])} onSelect={onEvidence}/>{checkText(value.formula) && <pre>{checkText(value.formula)}</pre>}</div>)}</>}
          {periods.length>0 && <><h4>Периоды дисконтирования</h4>{periods.map((value,i)=><div className="sensitivity-period" key={i}><p>Период: {checkText(value.value) || 'не установлен'}</p><Evidence items={checkEvidence([value.discount_cell,...checkEvidence(value.evidence)])} onSelect={onEvidence}/></div>)}</>}
          {item.recalculated_workbook===false && <p>Книга Excel целиком не пересчитывалась.</p>}
          {checkLimits(item.limitations).length>0 && <ul>{checkLimits(item.limitations).map((text,i)=><li key={i}>{text}</li>)}</ul>}
        </details>
      </article>;
    })}
    {scenarios.length===0 && <p>Поддержанные сценарии не представлены.</p>}
    {checks.length>0 && <details className="check-coverage"><summary>Охват и пропущенные выходы · {checks.length}</summary>{checks.map((item,index)=><div className="sensitivity-check" key={index}><p><strong>{CHECK_ITEM_STATUS[item.status] || 'Статус не указан'}</strong> · {SENSITIVITY_REASONS[item.reason] || 'Расчёт не поддерживается для этого выхода.'}</p><Evidence items={checkEvidence([item.target,item.source_target])} onSelect={onEvidence}/></div>)}</details>}
    {limits.length>0 && <div className="limitations"><h3>Границы сценариев</h3><ul>{limits.map((text,index)=><li key={index}>{text}</li>)}</ul></div>}
  </section>;
}

export function CheckPanel({title, data, onEvidence}) {
  if (!checkObject(data)) return null;
  const findings = Array.isArray(data.findings) ? data.findings.filter(checkObject) : [];
  const checks = Array.isArray(data.checks) ? data.checks.filter(checkObject) : [];
  const limits = checkLimits(data.limitations);
  const counts = Object.entries(CHECK_COUNTS).filter(([key])=>Number.isInteger(data.summary?.[key]) && data.summary[key]>=0);
  return <section className="check-panel" aria-label={title}>
    <div className="section-heading"><h2>{title}</h2><span className="check-run-status">{CHECK_STATUS[data.status] || 'Статус не указан'}</span></div>
    <p className="check-note">Финансовая существенность замечаний не оценена.</p>
    <div className="check-counts"><span>Замечаний в отчёте: {findings.length}</span>{counts.map(([key,label])=><span key={key}>{label}: {data.summary[key]}</span>)}</div>
    {data.summary?.truncated===true && <p className="check-note">Показана часть замечаний.</p>}
    {data.summary?.upstream_complete===false && <p className="check-note">Исходная проверка выполнена не полностью.</p>}
    {findings.map((item,i)=><CheckFinding key={item.id || i} item={item} onEvidence={onEvidence}/>)}
    {findings.length===0 && <p className="check-empty">Замечания не представлены.</p>}
    {checks.length>0 && <details className="check-coverage"><summary>Охват проверок · {checks.length}</summary><ul>{checks.map((item,i)=><li key={i}><span>{CHECK_ITEM_STATUS[item.status] || 'Статус не указан'}</span><p>{checkText(item.detail) || checkText(item.title) || `Проверка ${i+1}`}</p><Evidence items={checkEvidence(item.evidence)} onSelect={onEvidence}/></li>)}</ul></details>}
    {limits.length>0 && <div className="limitations"><h3>Границы проверки</h3><ul>{limits.map((text,i)=><li key={i}>{text}</li>)}</ul></div>}
  </section>;
}

function CheckFinding({item,onEvidence}) {
  return <article className="check-finding">
    <div className="observation-heading"><h3>{checkText(item.title) || 'Замечание без названия'}</h3><span className={`check-finding-status ${item.status==='candidate'?'candidate':''}`}>{item.status==='candidate'?'Требует проверки':item.status==='observation'?'Наблюдение':'Статус не указан'}</span></div>
    {checkText(item.detail) && <p>{checkText(item.detail)}</p>}
    <Evidence items={checkEvidence(item.evidence)} onSelect={onEvidence}/>
    <CheckCalculation data={item.calculation} onEvidence={onEvidence}/>
  </article>;
}

function Observation({item,index,onEvidence}) {
  return <article className="observation"><div className="observation-index">{String(index+1).padStart(2,'0')}</div><div>
    <div className="observation-heading"><h3>{item.title}</h3><span className="observation-type">{{observation:'Наблюдение',hypothesis:'Гипотеза',not_verified:'Не проверено'}[item.status] || item.status}</span></div>
    <p>{item.detail}</p><ComparisonDetails data={item.comparison} onEvidence={onEvidence}/>
    {item.impact && <p className="impact"><strong>Влияние:</strong> {item.impact==='not_assessed'?'Не оценено':item.impact}</p>}
    <Evidence items={item.evidence} onSelect={onEvidence}/>
  </div></article>;
}

export function ReportView({report, onEvidence}) {
  const [active, setActive] = useState('audit');
  const tabRefs=useRef({});
  const stagePanel=useRef(null), initialStage=useRef(true);
  useEffect(()=>{if(initialStage.current){initialStage.current=false;return;}stagePanel.current?.scrollIntoView?.({block:'start'});},[active]);
  const stage = report.stages?.find(s => s.id === active);
  const summary = report.summary || {};
  const unfinished = report.stages?.filter(s=>s.completion === 'incomplete') || [];
  const view=checkObject(report.presentation)?report.presentation:null;
  const stageView=view?.stage_views?.[active];
  const selectedIds=Array.isArray(stageView?.fact_ids)?new Set(stageView.fact_ids):null;
  const facts=Array.isArray(report.facts)?report.facts.filter(fact=>fact && fact.stage_id===active && (!selectedIds || selectedIds.has(fact.id))):[];
  const collapseIndexes=new Set(Array.isArray(stageView?.collapsed_observation_indexes)?stageView.collapsed_observation_indexes:[]);
  const observations=Array.isArray(stage?.observations)?stage.observations:[];
  const collapsed=observations.map((item,index)=>({item,index})).filter(({item,index})=>collapseIndexes.has(index) && isShownFactDuplicate(item,facts));
  const hidden=new Set(collapsed.map(({index})=>index));
  function moveTab(event,index){
    let next;
    if(event.key==='ArrowRight')next=(index+1)%STAGES.length;
    else if(event.key==='ArrowLeft')next=(index+STAGES.length-1)%STAGES.length;
    else if(event.key==='Home')next=0;
    else if(event.key==='End')next=STAGES.length-1;
    else return;
    event.preventDefault();setActive(STAGES[next][0]);tabRefs.current[STAGES[next][0]]?.focus();
  }
  const stats=<div className="stats-strip"><div><strong>{summary.sheet_count ?? '—'}</strong><span>листов</span></div><div><strong>{displayValue(summary.formula_cells ?? 0)}</strong><span>формульных ячеек</span></div><div><strong>{summary.cached_errors ?? 0}</strong><span>сохранённых ошибок</span></div><div><strong>{report.stages?.length ?? 4}</strong><span>раздела отчёта</span></div></div>;
  return <div className="report-view">
    {unfinished.length > 0 && <p className="notice" role="status">Частичный отчёт: не завершены разделы «{unfinished.map(s=>STAGES.find(([id])=>id===s.id)?.[1] || s.title).join('», «')}». Причины указаны в соответствующих разделах.</p>}
    <div className="report-tabs" role="tablist" aria-label="Разделы отчёта">{STAGES.map(([id,title], i) => <button key={id} ref={node=>{tabRefs.current[id]=node;}} id={`report-tab-${id}`} role="tab" aria-selected={active===id} aria-controls={`report-panel-${id}`} tabIndex={active===id?0:-1} onClick={()=>setActive(id)} onKeyDown={event=>moveTab(event,i)}><span>0{i+1}</span>{title}</button>)}</div>
    {view?<><ReportOverview view={view} onEvidence={onEvidence}/><KeyMetrics items={view.metrics} onEvidence={onEvidence}/></>:<>{stats}<p className="scope-note">Наличие ссылки подтверждает источник. Финансовое влияние замечания требует отдельной проверки. Сохранённые значения могут отличаться от свежего пересчёта.</p></>}
    {!view && <><CheckPanel title="Финансовые проверки" data={report.financial_checks} onEvidence={onEvidence}/><CheckPanel title="Дополнительные проверки Excel" data={report.supplemental_audit} onEvidence={onEvidence}/><ValuationSensitivityPanel data={report.valuation_sensitivity} onEvidence={onEvidence}/></>}
    {STAGES.filter(([id])=>id!==active).map(([id])=><section key={id} id={`report-panel-${id}`} role="tabpanel" aria-labelledby={`report-tab-${id}`} hidden/>)}
    <section ref={stagePanel} className="stage-body" id={`report-panel-${active}`} role="tabpanel" aria-labelledby={`report-tab-${active}`} tabIndex={0}>
      <div className="section-heading"><h2>{stage?.title || STAGES.find(s => s[0] === active)?.[1]}</h2>{stage && <span className={`status ${stage.status}`}>{STATUS[stage.status] || stage.status}</span>}</div>
      {active==='quality' && report.decision_assessment ? <><DecisionPanel data={report.decision_assessment} onEvidence={onEvidence} conclusionOnly/><details><summary>Исходное резюме стадии</summary><p>{stage?.summary}</p></details></> : <p className="stage-summary">{stage?.summary || 'Этот раздел ещё не сформирован.'}</p>}
      {active==='audit' && <DecisionPanel data={report.decision_assessment} onEvidence={onEvidence}/>}
      {view && active==='audit' && <FinancialGroups items={view.financial_groups} onEvidence={onEvidence} renderFinding={item=><CheckFinding item={item} onEvidence={onEvidence}/>}/>} 
      {active==='audit' && <ImpactPanel data={report.impact_analysis} onEvidence={onEvidence}/>}
      {view && active==='business' && <><ComparisonCharts items={view.comparison_charts} onEvidence={onEvidence} renderDetails={data=><ComparisonDetails data={data} onEvidence={onEvidence}/>}/><SensitivityCharts items={view.sensitivity_charts} onEvidence={onEvidence} renderScenarios={scenarios=><ValuationSensitivityPanel data={{status:'limited',scenarios}} onEvidence={onEvidence}/>}/></>}
      <FactPanel items={facts} onEvidence={onEvidence}/>
      {observations.map((item,index)=>!hidden.has(index) && <Observation key={index} item={item} index={index} onEvidence={onEvidence}/>)}
      {collapsed.length>0 && <details className="technical original-observations"><summary>Исходные наблюдения к показанным фактам · {collapsed.length}</summary><p>Эти сохранённые наблюдения повторяют значения в таблице. Их источники и формулировки доступны здесь.</p>{collapsed.map(({item,index})=><Observation key={index} item={item} index={index} onEvidence={onEvidence}/>)}</details>}
      {stage?.limitations?.length > 0 && <div className="limitations"><h3>Границы проверки</h3><ul>{stage.limitations.map((t,i) => <li key={i}>{t}</li>)}</ul></div>}
    </section>
    <CalculationPanel items={report.calculations?.filter(item=>item.stage_id === active)} onEvidence={onEvidence}/>
    {view && <div className="report-technical"><h2>Подробности проверки</h2>
      <details className="technical"><summary>Книга и охват анализа</summary>{stats}<p>Количество сохранённых ошибок не оценивает финансовую корректность модели.</p></details>
      {Array.isArray(report.facts) && report.facts.length>0 && <details className="technical"><summary>Все сохранённые факты · {report.facts.length}</summary><FactPanel items={report.facts.filter(checkObject)} onEvidence={onEvidence}/></details>}
      {report.financial_checks && <details className="technical"><summary>Финансовые проверки — полный состав и ограничения</summary><CheckPanel title="Исходные финансовые проверки" data={report.financial_checks} onEvidence={onEvidence}/></details>}
      {report.supplemental_audit && <details className="technical"><summary>Дополнительные проверки Excel · {CHECK_STATUS[report.supplemental_audit.status] || 'Статус не указан'}</summary><CheckPanel title="Дополнительные проверки Excel" data={report.supplemental_audit} onEvidence={onEvidence}/></details>}
      {report.valuation_sensitivity && <details className="technical"><summary>Сценарии — полный состав и пропущенные выходы</summary><ValuationSensitivityPanel data={report.valuation_sensitivity} onEvidence={onEvidence}/></details>}
    </div>}
    <details className="technical"><summary>Структурные находки · {report.structural?.findings?.length || 0}</summary>
      {report.structural?.findings?.map((f,i) => <article key={i}><h3>{f.title}</h3><p>{f.impact}</p><Evidence items={(f.cells || []).map(cell => ({sheet:f.sheet,cell})).filter(e => e.sheet)} onSelect={onEvidence}/></article>)}
    </details>
  </div>;
}

function CellDrawer({data, error, reference, onClose}) {
  const cell = data?.cells?.find(c => (c.cell || c.address) === reference.cell) || data?.cells?.[0];
  const closeButton = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    closeButton.current?.focus();
    const handle=e=>{if(e.key==='Escape')onClose();if(e.key==='Tab'){e.preventDefault();closeButton.current?.focus();}};
    window.addEventListener('keydown',handle);
    return()=>{window.removeEventListener('keydown',handle);previous?.focus();};
  },[onClose]);
  return <div className="drawer-backdrop" onClick={onClose}><aside className="drawer" role="dialog" aria-modal="true" aria-label="Источник в Excel" onClick={e => e.stopPropagation()}>
    <div className="section-heading"><span className="eyebrow">ИСТОЧНИК В EXCEL</span><button ref={closeButton} className="icon-button" onClick={onClose} aria-label="Закрыть источник"><Icon name="close"/></button></div>
    <h2>{reference.sheet}</h2><div className="cell-address">{reference.cell}</div>
    {error ? <p className="error" role="alert">{error}</p> : !data ? <p>Читаем ячейку…</p> : <>
      <label className="detail-label">Сохранённое значение</label><div className="cell-value">{displayValue(cell?.value)}</div>
      {typeof cell?.value==='number' && <p className="exact-value">Точное сохранённое значение: <code>{String(cell.value)}</code></p>}
      <label className="detail-label">Формула</label><pre>{cell?.formula || (cell?.formula_type ? 'Формула недоступна в этом представлении' : 'Формула отсутствует')}</pre>
      {cell?.row_context && <><label className="detail-label">Подписи в строке</label><pre>{JSON.stringify(cell.row_context,null,2)}</pre></>}
      {cell?.column_context && <><label className="detail-label">Контекст периода</label><pre>{JSON.stringify(cell.column_context,null,2)}</pre></>}
      <p className="data-note">Прочитано из сохранённого файла. Исходная книга не изменяется.</p>
    </>}
  </aside></div>;
}

export default function App() {
  const [health,setHealth]=useState(null),[jobs,setJobs]=useState([]),[selected,setSelected]=useState(null);
  const [report,setReport]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
  const [evidence,setEvidence]=useState(null),[cell,setCell]=useState(null),[cellError,setCellError]=useState('');
  const evidenceRequest = useRef(0);
  const closeEvidence = useCallback(()=>{evidenceRequest.current++;setEvidence(null);},[]);
  const selectedId = selected?.id;
  useEffect(() => {let live=true; const refresh=async()=>{try{const [h,j]=await Promise.all([api('/health'),api('/jobs')]);if(live){setHealth(h);setJobs(j.jobs || []);}}catch(e){if(live)setError(e.message);}};refresh();const timer=setInterval(refresh,5000);return()=>{live=false;clearInterval(timer);};},[]);
  useEffect(() => {
    if (!selectedId) {setReport(null);return;}
    let live=true, timer; setReport(null);setLoading(true);
    const refresh=async()=>{
      let terminal=false;
      try{
        const job=await api(`/jobs/${selectedId}`);if(!live)return;setSelected(job);
        if(job.report_available){const r=await api(`/jobs/${selectedId}/report`);if(live)setReport(r);}
        terminal=!['queued','running'].includes(job.status);
        if(live)setError('');
      }catch(e){if(live)setError(e.message);}
      finally{if(live){setLoading(false);if(!terminal)timer=setTimeout(refresh,2000);}}
    };
    refresh();return()=>{live=false;clearTimeout(timer);};
  },[selectedId]);
  async function action(name){try{const job=await api(`/jobs/${selectedId}/${name}`,{method:'POST'});setSelected(null);setTimeout(()=>setSelected(job),0);setError('');}catch(e){setError(e.message);}}
  async function showEvidence(ref){const request=++evidenceRequest.current;setEvidence(ref);setCell(null);setCellError('');try{const result=await api(`/jobs/${selectedId}/cells?${new URLSearchParams({sheet:ref.sheet,range:ref.cell})}`);if(request===evidenceRequest.current)setCell(result);}catch(e){if(request===evidenceRequest.current)setCellError(e.message);}}
  const running=['queued','running'].includes(selected?.status);
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><div className="brand-symbol">ƒ</div><div>Финансовый аудит<small>Рабочее пространство</small></div></div>
      <button className="new-analysis" onClick={()=>{setSelected(null);setError('');}}>+ Новый анализ</button>
      <div className="sidebar-label">ИСТОРИЯ ПРОВЕРОК</div>
      <nav aria-label="История анализов" className="job-list">{jobs.length===0?<p className="empty-history">Здесь появятся ваши модели и результаты проверок.</p>:jobs.map(job=><button key={job.id} className={`job-item ${selectedId===job.id?'selected':''}`} onClick={()=>{setSelected(job);setError('');}}><Icon name="file"/><span><strong>{job.filename}</strong><small><i className={`status-dot ${job.status}`}/>{STATUS[job.status] || job.status}</small></span></button>)}</nav>
      <div className="sidebar-footer"><span className={`connection-dot ${health?'connected':''}`}/>{health?'Локальный сервис работает':'Подключаемся к сервису'}<small>Учебный проект · GigaChat</small></div>
    </aside>
    <main className="main"><header className="topbar"><span>{selected?'Результаты анализа':'Проверка финансовой модели'}</span><span className="local-label">Локальное пространство</span></header>
      <div className="main-content">{error&&<div className="error global-error" role="alert">{error}<button onClick={()=>setError('')} aria-label="Закрыть сообщение">×</button></div>}
      {!selected?<><UploadPanel configured={health?.provider?.configured} onCreated={job=>{setSelected(job);setJobs(prev=>[job,...prev]);}}/>
        <section className="workflow-explainer"><h3>От исходных формул к заключению</h3><div>{STAGES.map(([id,title],i)=><div key={id}><span>0{i+1}</span><strong>{title}</strong><p>{['Формулы, связи и вопросы для проверки','Предпосылки и ограничения проверки','Условия использования модели','Показатели, сравнения и локальные сценарии'][i]}</p></div>)}</div></section></>:
        <><div className="report-header"><div><div className="eyebrow">ФИНАНСОВАЯ МОДЕЛЬ</div><h1>{selected.filename}</h1><div className="report-meta"><span className={`status ${selected.status}`}>{STATUS[selected.status]}</span><span>{selected.mode==='local'?'Локальная проверка':(report?.provider?.version || report?.provider?.model || 'GigaChat')}</span></div></div><div className="report-actions">{selected.report_available&&<><a className="secondary" href={`/api/jobs/${selectedId}/download?format=html`}>Скачать отчёт</a><PptxDownload jobId={selectedId}/><a className="text-link" href={`/api/jobs/${selectedId}/download?format=json`}>JSON</a></>}{running&&<button className="secondary" onClick={()=>action('cancel')}>Остановить</button>}{['failed','cancelled'].includes(selected.status)&&<button className="primary" onClick={()=>action('retry')}>Повторить анализ</button>}</div></div>
          {running&&<section className="progress-section"><div className="progress-heading"><strong>{selected.status==='queued'?'Модель ожидает запуска':'Агент изучает модель'}</strong><span>{typeof selected.progress==='number'?`${Math.round(selected.progress)}%`:''}</span></div><div className="progress-track"><div style={{width:`${Math.max(3,Math.min(100,Number(selected.progress)||0))}%`}}/></div><ol>{STAGES.map(([id,title],i)=><li key={id} className={selected.stage===id?'current':''}><span>{i+1}</span>{title}</li>)}</ol><p>Результаты сохраняются по этапам. Можно продолжать работу в другой вкладке.</p></section>}
          {selected.error&&<p className="error" role="alert">{selected.error}</p>}
          {report?<ReportView key={selectedId} report={report} onEvidence={showEvidence}/>:!running&&<p className="empty-result">{loading?'Загружаем результат…':'Отчёт ещё не сформирован.'}</p>}
        </>}
      </div><footer className="main-footer">Выводы с источниками. Оригинальные файлы сохраняются.</footer>
    </main>
    {evidence&&<CellDrawer reference={evidence} data={cell} error={cellError} onClose={closeEvidence}/>}
  </div>;
}
