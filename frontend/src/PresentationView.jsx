import React, {useId} from 'react';
import SourceLinks from './SourceLinks.jsx';
import {finite, readableNumber, ValueBars} from './ReportCharts.jsx';

const list = value => Array.isArray(value) ? value : [];
const text = value => typeof value === 'string' ? value : '';
const statusLabel = value => ({limited:'С ограничениями',not_verified:'Не проверено',incomplete:'Частичный отчёт',completed:'Отчёт сформирован',complete:'Отчёт сформирован'}[value] || 'Границы проверки указаны в отчёте');
const periodType = value => ({actual:'Факт',forecast:'Прогноз'}[value] || 'Факт/прогноз не определён');
const bindingLabel = value => ({bound:'Контекст связан',partial:'Контекст неполный',ambiguous:'Неоднозначная связь',unsupported:'Значение не поддерживается'}[value] || 'Связь не установлена');

export function ReportOverview({view, onEvidence}) {
  const overview = view.overview || {};
  return <section className="report-overview" aria-label="Обзор результатов">
    <div className="overview-heading"><div><div className="eyebrow">ОБЗОР РЕЗУЛЬТАТОВ</div><h2>{text(overview.title) || 'Что показывает проверка'}</h2></div><span className="overview-status">{statusLabel(overview.status)}</span></div>
    <div className="overview-columns"><div className="overview-summary">{list(overview.summary).filter(x=>typeof x==='string').map((line,index)=><p key={index}>{line}</p>)}</div>
      {list(overview.next_steps).length>0 && <div className="next-steps"><h3>Что проверить дальше</h3><ol>{overview.next_steps.map((step,index)=><li key={index}><p>{text(step.text)}</p><SourceLinks items={step.evidence} onSelect={onEvidence} maxVisible={2}/></li>)}</ol></div>}
    </div>
    {list(view.notices).length>0 && <details className="overview-boundaries"><summary>Как читать результаты</summary><ul>{view.notices.map((notice,index)=><li key={index}>{text(notice)}</li>)}</ul></details>}
  </section>;
}

export function KeyMetrics({items, onEvidence}) {
  if (!list(items).length) return null;
  return <section className="key-metrics" aria-label="Ключевые факты"><div className="metrics-heading"><h2>Ключевые факты</h2><span>Сохранённые значения</span></div>
    <div className="metric-grid">{items.map((metric,index)=><article className="metric-card" key={`${metric.fact_id}-${index}`}>
      <h3>{text(metric.label) || 'Подпись не определена'}</h3>
      <button className="metric-value" type="button" disabled={!metric.source_sheet || !metric.value_cell} aria-label={`Сохранённое значение: ${text(metric.value_label) || readableNumber(metric.value)}. Источник ${metric.source_sheet || ''}!${metric.value_cell || ''}`} onClick={()=>onEvidence({sheet:metric.source_sheet,cell:metric.value_cell})}>{text(metric.value_label) || readableNumber(metric.value)}</button>
      <div className="metric-context"><span>{text(metric.period_label) || (metric.period==null?'Период не определён':String(metric.period))}</span><span>{text(metric.period_type_label) || periodType(metric.period_type)}</span></div>
      <p className="metric-unit">Подпись единицы: <strong>{text(metric.unit) || 'не определена'}</strong></p>
      <p className="metric-provenance">Лист: {text(metric.source_sheet) || 'не указан'}</p>
      <span className={`fact-binding ${metric.binding_status==='bound'?'bound':'uncertain'}`}>{bindingLabel(metric.binding_status)}</span>
      <details className="metric-sources"><summary>Связь с источниками</summary><SourceLinks items={metric.evidence} onSelect={onEvidence}/></details>
    </article>)}</div>
  </section>;
}

export function FinancialGroups({items, onEvidence, renderFinding}) {
  if (!list(items).length) return null;
  return <section className="financial-groups" aria-label="Финансовые проверки"><div className="section-heading"><h2>Финансовые проверки</h2><span className="section-subtitle">{items.length} групп замечаний</span></div>
    <p className="check-note">Финансовая существенность замечаний не оценена.</p>
    <div className="finding-groups">{items.map((group,index)=><article className="finding-group" key={group.id || index}>
      <div className="observation-heading"><h3>{text(group.title) || 'Замечание к расчётам'}</h3><span className="check-finding-status candidate">{group.status==='observation'?'Наблюдение':'Требует проверки'}</span></div>
      {text(group.detail) && <p>{group.detail}</p>}
      {text(group.consequence) && <div className="finding-context"><strong>Почему важно проверить</strong><p>{group.consequence}</p></div>}
      {text(group.action) && <div className="finding-action"><strong>Следующий шаг</strong><p>{group.action}</p></div>}
      <SourceLinks items={group.evidence} onSelect={onEvidence} maxVisible={4}/>
      <details className="group-originals"><summary>Исходные замечания · {list(group.findings).length}</summary>{list(group.findings).map((finding,i)=><React.Fragment key={finding.id || i}>{renderFinding(finding,i)}</React.Fragment>)}</details>
    </article>)}</div>
  </section>;
}

export function ComparisonCharts({items, onEvidence, renderDetails}) {
  const charts = list(items).filter(chart => Array.isArray(chart.points) && chart.points.length===2 && chart.points.every(point=>point && point.period!=null && finite(point.value)));
  if (!charts.length) return null;
  return <section className="comparison-charts" aria-label="Сравнение выбранных периодов"><div className="section-heading"><h2>Сравнение выбранных периодов</h2></div><div className="chart-grid">{charts.map((chart,index)=><EndpointChart key={chart.id || index} chart={chart} onEvidence={onEvidence} renderDetails={renderDetails}/>)}</div></section>;
}

function EndpointChart({chart,onEvidence,renderDetails}) {
  const titleId = useId();
  return <figure className="chart-card" aria-labelledby={titleId}><figcaption id={titleId}><h3>{text(chart.title) || text(chart.metric_label) || 'Выбранные значения'}</h3><p>Подпись единицы: {text(chart.unit) || 'не определена'} · лист: {text(chart.source_sheet)}</p></figcaption>
    <ValueBars points={chart.points}/>
    <table className="chart-data"><caption className="visually-hidden">Сохранённые значения и их источники</caption><thead><tr><th scope="col">Период</th><th scope="col">Сохранённое значение</th></tr></thead><tbody>{chart.points.map((point,index)=><tr key={index}><th scope="row">{text(point.label) || String(point.period)}<small>{text(point.period_type_label) || periodType(point.period_type)}</small></th><td><button type="button" className="chart-source" onClick={()=>onEvidence({sheet:point.sheet,cell:point.cell})} aria-label={`${point.label || point.period}: ${readableNumber(point.value)}. Источник ${point.sheet}!${point.cell}`}>{readableNumber(point.value)}<small>{point.sheet}!{point.cell}</small></button></td></tr>)}</tbody></table>
    <dl className="comparison-results">{list(chart.comparisons).map((comparison,index)=>finite(comparison.display?.value) && <div key={index}><dt>{{total_growth:'Изменение за период',cagr:'Среднегодовой темп (CAGR)',difference:'Разница'}[comparison.kind] || 'Расчёт'}</dt><dd>{readableNumber(comparison.display.value)} <span>{text(comparison.display.unit)}</span></dd></div>)}</dl>
    <p className="chart-note">{text(chart.note) || 'Показаны только выбранные конечные точки. Промежуточная динамика не восстановлена.'}</p>
    <details className="chart-method"><summary>Расчёты и источники</summary>{list(chart.comparisons).map((comparison,index)=><React.Fragment key={index}>{renderDetails(comparison)}</React.Fragment>)}<SourceLinks items={chart.evidence} onSelect={onEvidence}/></details>
  </figure>;
}

export function SensitivityCharts({items,onEvidence,renderScenarios}) {
  const charts=list(items).filter(chart=>Array.isArray(chart.points) && chart.points.some(point=>point && finite(point.value) && finite(point.delta)));
  if(!charts.length)return null;
  return <section className="sensitivity-charts" aria-label="Чувствительность оценки"><div className="section-heading"><h2>Чувствительность оценки</h2><span className="section-subtitle">Локальные сценарии исходной формулы</span></div>
    <p className="chart-note">Незатронутые входы и FCFF зафиксированы. Полный пересчёт Excel не выполнялся.</p>
    <div className="chart-grid">{charts.map((chart,index)=><SensitivityChart key={chart.id || index} chart={chart} onEvidence={onEvidence} renderScenarios={renderScenarios}/>)}</div>
  </section>;
}

function SensitivityChart({chart,onEvidence,renderScenarios}) {
  const titleId=useId();
  const points=chart.points.filter(point=>point && finite(point.value) && finite(point.delta));
  return <figure className="chart-card sensitivity-chart" aria-labelledby={titleId}><figcaption id={titleId}><h3>{text(chart.title) || 'Чувствительность выбранного выхода'}</h3><p>Подпись единицы выхода: {text(chart.unit) || 'не определена'}</p></figcaption>
    <div className="scenario-baseline"><span>Выход при исходных входах</span><strong>{readableNumber(chart.baseline)}</strong><SourceLinks items={[chart.target]} onSelect={onEvidence}/></div>
    <p className="chart-axis-label">Изменение выхода относительно исходного</p><ValueBars points={points} valueKey="delta" label="Изменение выхода"/>
    <div className="table-scroll"><table className="chart-data"><caption className="visually-hidden">Значения сценариев</caption><thead><tr><th scope="col">Сценарий</th><th scope="col">Выход</th><th scope="col">Изменение</th></tr></thead><tbody>{points.map((point,index)=><tr key={index}><th scope="row">{text(point.label) || (finite(point.change_pp)?`${point.change_pp>0?'+':''}${readableNumber(point.change_pp)} п.п.`:'Изменение не установлено')}</th><td>{readableNumber(point.value)}</td><td>{point.delta>0?'+':''}{readableNumber(point.delta)}</td></tr>)}</tbody></table></div>
    <p className="chart-note">{text(chart.note)}</p>
    <details className="chart-method"><summary>Формулы и источники сценариев</summary><SourceLinks items={chart.evidence} onSelect={onEvidence}/>{renderScenarios(list(chart.scenarios))}</details>
  </figure>;
}

// A server-provided collapse index is honored only while its exact source fact
// is in the visible stage table. Comparisons and free prose remain visible.
export function isShownFactDuplicate(observation, facts) {
  if (observation?.narrative_verification!=='source_fact_text_generated' || observation.comparison || !Array.isArray(observation.fact_claims) || !observation.fact_claims.length) return false;
  const fields=['metric_label','period','unit','source_sheet','value','value_cell','binding_status'];
  return observation.fact_claims.every(claim=>facts.some(fact=>fact.id===claim.fact_id && fields.every(key=>fact[key]===claim[key])));
}
