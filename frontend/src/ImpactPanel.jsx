import React from 'react';
import SourceLinks from './SourceLinks.jsx';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const text = value => typeof value === 'string' ? value : '';
const list = value => Array.isArray(value) ? value : [];
const strings = value => list(value).filter(item => typeof item === 'string');
const ref = value => object(value) && typeof value.sheet === 'string' && value.sheet.length > 0 && typeof value.cell === 'string' && /^[A-Z]{1,3}[1-9][0-9]*$/.test(value.cell);
const key = value => JSON.stringify([value.sheet, value.cell]);
const close = (a,b,rel=1e-9,abs=1e-9) => finite(a) && finite(b) && Math.abs(a-b) <= Math.max(abs,rel*Math.max(Math.abs(a),Math.abs(b)));
const changedFormula = patch => text(patch.changed_formula) || text(patch.scenario_formula);
const patchSources = patch => [...list(patch.excluded),...list(patch.replaced_sources),...list(patch.replacement_sources)];
const readable = value => finite(value) ? new Intl.NumberFormat('ru-RU',{maximumSignificantDigits:12}).format(value) : 'Не установлено';
const signed = value => `${value>0?'+':''}${readable(value)}`;

const STATUS = {limited:'Рассчитано с ограничениями',unsupported:'Нет поддержанных сценариев',not_applicable:'Сценарии не применимы',partial:'Рассчитано частично',unavailable:'Расчёт недоступен'};
const CHECK_STATUS = {calculated:'Рассчитан условный сценарий',unsupported:'Сценарий не поддерживается',not_applicable:'Не применим'};
const REASONS = {
  baseline_does_not_match_saved_output:'Исходный расчёт не совпадает с сохранённым выходом.',
  baseline_intermediate_cache_mismatch:'Промежуточный расчёт не совпадает с сохранёнными значениями.',
  missing_or_ambiguous_output_unit:'Подпись единицы выхода отсутствует или неоднозначна.',
  no_unique_supported_conditional_output_patch:'Для замечания не установлена однозначная условная замена формулы.',
  no_supported_path_to_labelled_ev_npv:'Не установлен поддерживаемый путь от изменения до EV или NPV.',
  no_supported_financial_candidates:'Подходящих исходных замечаний для сценариев нет.',
  unresolved_affected_dependency:'Не все затронутые зависимости разрешены.',
  overlap_not_established_for_all_selected_flows:'Совпадение состава прибыли подтверждено не для всех выбранных потоков.',
  overlap_rows_not_one_consistent_series:'Выбранные потоки не образуют один согласованный ряд.',
  overlap_intervals_not_strictly_increasing:'Последовательность интервалов потоков неоднозначна.',
  overlap_formula_series_not_consistent:'Формулы выбранного ряда различаются по структуре.',
  gordon_output_not_unique_single_extra_discount:'Не установлен единственный дополнительный шаг дисконтирования.',
  gordon_saved_pv_does_not_match_flow_factor:'Сохранённый PV не совпадает с произведением потока и коэффициента.',
  ambiguous_gordon_numerator_patch:'Замена числителя терминальной стоимости неоднозначна.',
  missing_or_nonfinite_proof_source:'Исходное значение отсутствует или не является конечным числом.',
  nonfinite_impact:'Расчёт не дал конечного числового результата.',
  target_limit:'Достигнут предел числа рассматриваемых выходов.',
  scenario_limit:'Достигнут предел числа сценариев.',
  impact_proof_cell_limit:'Полный состав источников превышает установленный предел.',
  impact_atomic_payload_limit:'Полное доказательство сценария превышает установленный объём.',
};

// Presentation checks only: this does not verify the financial assumption or
// execute a formula. The engine remains responsible for the source-bound path.
export function impactScenarioIssue(item) {
  if (!object(item) || item.conditional !== true || item.recalculated_workbook !== false || (item.status !== undefined && item.status !== 'calculated')) return 'Не подтверждены границы условного сценария.';
  if (!['baseline','saved_output','result','delta'].every(name => finite(item[name]))) return 'Числовые результаты сценария неполны или некорректны.';
  if (!close(item.result-item.baseline,item.delta) || !close(item.baseline,item.saved_output,1e-7,1e-6)) return 'Числа сценария не согласованы с исходным расчётом.';
  if (item.baseline === 0 ? item.relative_delta !== null : !close(item.relative_delta,item.delta/Math.abs(item.baseline))) return 'Относительное изменение не согласовано с исходной базой.';
  if (item.baseline_cache_difference != null && !close(item.baseline_cache_difference,item.baseline-item.saved_output)) return 'Разница исходного расчёта и сохранённого выхода не согласована.';
  if (!ref(item.target) || !text(item.raw_unit).trim()) return 'Источник выхода или подпись единицы не установлены.';
  const patches=list(item.patches), path=list(item.formula_path), inputs=list(item.source_inputs);
  if (!patches.length || patches.some(p => !ref(p) || !text(p.original_formula) || !changedFormula(p) || (text(p.changed_formula) && text(p.scenario_formula) && p.changed_formula !== p.scenario_formula))) return 'Исходная и условная формулы представлены неполностью.';
  if (!path.length || path.some(p => !ref(p) || !text(p.formula)) || !inputs.length || inputs.some(p => !ref(p) || !finite(p.value) || p.frozen !== true)) return 'Путь расчёта или зафиксированные входы представлены неполностью.';
  const sources=new Set(list(item.evidence).filter(ref).map(key));
  if ([item.target,...patches,...path,...inputs,...patches.flatMap(patchSources)].some(p => !ref(p) || !sources.has(key(p)))) return 'Не все показанные формулы и входы связаны с источниками.';
  return null;
}

function Limits({items}) {
  return strings(items).length ? <ul>{strings(items).map((item,i)=><li key={i}>{item}</li>)}</ul> : null;
}

function ScenarioDetails({item,onEvidence}) {
  return <details className="check-calculation">
    <summary><span>Допущение, изменённые формулы и зафиксированные входы</span><small style={{display:'block',marginTop:4}}>{text(item.title) || 'Условный сценарий'}</small></summary>
    <p>{text(item.assumption) || 'Допущение не описано.'}</p>
    <p>Сохранённый выход: {String(item.saved_output)}. Исходный расчёт: {String(item.baseline)}. Условный выход: {String(item.result)}. Изменение: {String(item.delta)}.</p>
    {item.baseline===0 ? <p>Относительное изменение не определено: исходная база равна нулю.</p> : <p>Относительное изменение к модулю исходного расчёта: {finite(item.relative_delta*100)?`${signed(item.relative_delta*100)}%`:`${signed(item.relative_delta)} (доля)`}.</p>}
    {item.baseline_cache_difference!=null && <p>Разница исходного расчёта и сохранённого выхода: {String(item.baseline_cache_difference)}.</p>}
    <h4>Изменённые формулы</h4>
    {item.patches.map((patch,i)=><div className="sensitivity-formula" key={i}>
      <SourceLinks items={[patch]} onSelect={onEvidence}/><p>Исходная формула</p><pre>{patch.original_formula}</pre>
      <p>Формула условного сценария</p><pre>{changedFormula(patch)}</pre>
      <SourceLinks items={patchSources(patch)} onSelect={onEvidence} maxVisible={6}/>
    </div>)}
    <h4>Исходный путь к выходу</h4>
    {item.formula_path.map((value,i)=><div className="sensitivity-formula" key={i}><SourceLinks items={[value]} onSelect={onEvidence}/><pre>{value.formula}</pre></div>)}
    <h4>Зафиксированные входы</h4>
    {item.source_inputs.map((value,i)=><div className="sensitivity-input" key={i}><p>Сохранённое значение: {String(value.value)} · зафиксировано</p><SourceLinks items={[value]} onSelect={onEvidence}/></div>)}
    <p>Пересчитан только указанный путь с заменой формул; остальные входы сохранены. Книга Excel целиком не пересчитывалась.</p>
    <SourceLinks items={item.evidence} onSelect={onEvidence} maxVisible={8}/>
    <Limits items={item.limitations}/>
  </details>;
}

export default function ImpactPanel({data,onEvidence}) {
  if (!object(data)) return null;
  const scenarios=list(data.scenarios), checks=list(data.checks).filter(object);
  return <section className="check-panel valuation-panel impact-panel" aria-label="Условное влияние на оценку">
    <div className="section-heading"><h2>Условное влияние на оценку</h2><span className="check-run-status">{STATUS[data.status] || 'Статус не указан'}</span></div>
    <p className="check-note">Условные альтернативы, рассчитанные кодом по исходной формуле. Эффекты отдельных сценариев не складываются; финансовая существенность не оценена.</p>
    <p className="check-note">Подписи единиц показаны как в модели. Сохранённые числа не масштабируются по формату Excel.</p>
    {scenarios.length>0 ? <><p className="check-note">На узком экране таблицу можно прокрутить вправо. Пояснения и формулы находятся под таблицей.</p><div className="table-scroll" role="region" aria-label="Таблица условных сценариев" tabIndex={0}>
      <table className="chart-data" style={{minWidth:640}}><caption style={{textAlign:'left'}}>Отдельные условные сценарии</caption>
        <thead><tr>{['Сценарий','Исходный расчёт','Условный выход','Изменение','Подпись единицы','Целевой выход'].map(label=><th scope="col" key={label} style={{padding:'10px 8px'}}>{label}</th>)}</tr></thead>
        <tbody>{scenarios.map((item,i)=>{
          const issue=impactScenarioIssue(item), title=object(item)?text(item.title):'';
          if (issue) return <tr key={i}><th scope="row">{title || 'Сценарий'}</th><td colSpan={5}>Расчёт не показан: {issue}</td></tr>;
          return <tr key={item.id || i}><th scope="row" style={{padding:'10px 8px',minWidth:200}}>{title || 'Условный сценарий'}</th>{['baseline','result','delta'].map(name=><td key={name} style={{padding:'10px 8px',whiteSpace:'nowrap'}}>{name==='delta'?signed(item[name]):readable(item[name])}</td>)}<td style={{padding:'10px 8px'}}>{item.raw_unit}</td><td style={{padding:'10px 8px'}}><SourceLinks items={[item.target]} onSelect={onEvidence}/></td></tr>;
        })}</tbody>
      </table></div>{scenarios.filter(item=>!impactScenarioIssue(item)).map((item,i)=><ScenarioDetails key={item.id || i} item={item} onEvidence={onEvidence}/>)}</> : <p className="check-empty">Поддержанные условные сценарии не представлены.</p>}
    {checks.length>0 && <details className="check-coverage"><summary>Охват и неподдержанные сценарии · {checks.length}</summary>{checks.map((check,i)=><div className="sensitivity-check" key={i}><p><strong>{CHECK_STATUS[check.status] || 'Статус не указан'}</strong>{check.status!=='calculated' && ` · ${REASONS[check.reason] || 'Для этой формулы не установлен поддерживаемый путь условного пересчёта.'}`}</p><SourceLinks items={[check.target,check.source,...list(check.evidence)].filter(ref)} onSelect={onEvidence} maxVisible={6}/></div>)}</details>}
    {strings(data.limitations).length>0 && <div className="limitations"><h3>Границы сценариев</h3><Limits items={data.limitations}/></div>}
  </section>;
}
