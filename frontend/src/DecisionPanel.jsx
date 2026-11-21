import React from 'react';
import SourceLinks from './SourceLinks.jsx';
import {readableNumber, finite} from './ReportCharts.jsx';

const list=value=>Array.isArray(value)?value:[];
const text=value=>typeof value==='string'?value:'';
const number=value=>finite(value)?readableNumber(value):'Не установлено';
const relativeLabel=value=>!finite(value)?'Не определяется при нулевой базе':finite(value*100)?`${number(value*100)}%`:`${number(value)} (доля)`;

export default function DecisionPanel({data,onEvidence,conclusionOnly=false}) {
  if (!data || typeof data!=='object') return null;
  const suitability=data.suitability || {};
  return <section className="decision-panel check-panel" aria-label={conclusionOnly?'Применение модели и необходимые проверки':'Приоритет проверки замечаний'}>
    <h2>{conclusionOnly?'Применение модели и необходимые проверки':'Приоритет проверки замечаний'}</h2>
    {conclusionOnly ? <>
      <p>{text(suitability.summary)}</p>
      <h3>Что позволяют полученные данные</h3><ul>{list(suitability.supported_uses).map((item,i)=><li key={i}>{text(item)}</li>)}</ul>
      <h3>Что пока не обосновано</h3><ul>{list(suitability.unsubstantiated_uses).map((item,i)=><li key={i}>{text(item)}</li>)}</ul>
      <h3>Необходимые проверки</h3><ul>{list(suitability.required_checks).map((item,i)=><li key={i}>{text(item)}</li>)}</ul>
    </> : <>
      <p className="check-note">Сравниваются отдельные условные эффекты для одного выхода, единицы и исходной базы. Альтернативы одного замечания объединены по максимальному модулю изменения. Их эффекты не складываются.</p>
      {list(data.outputs).map((output,index)=><article className="decision-output" key={output.id || index}>
        <h3>{text(output.label)} · {text(output.target?.sheet)}!{text(output.target?.cell)}</h3>
        <p>Исходный расчёт: {number(output.baseline)}. Подпись единицы: {text(output.raw_unit)}.</p>
        <div className="table-scroll" role="region" aria-label={`Приоритеты ${text(output.label)}`} tabIndex={0}><table className="chart-data"><thead><tr><th>Приоритет</th><th>Замечание</th><th>Макс. модуль изменения</th><th>К модулю исходной базы</th></tr></thead><tbody>
          {list(output.ranked_findings).map((finding,i)=><tr key={finding.code || i}><td>{Number.isInteger(finding.rank)?finding.rank:'—'}</td><th scope="row">{text(finding.title)}</th><td>{number(finding.max_absolute_delta)} {text(output.raw_unit)}</td><td>{relativeLabel(finding.max_absolute_relative_delta)}</td></tr>)}
        </tbody></table></div>
        {list(output.ranked_findings).map((finding,i)=><details key={finding.code || i}><summary>{text(finding.title)}: основание и проверка</summary><p>{text(finding.reason)}</p><p>{text(finding.action)}</p>
          {list(finding.alternatives).map((item,j)=><p key={item.id || j}>{text(item.title)}: условный выход {number(item.result)}; изменение {number(item.delta)} {text(output.raw_unit)}.</p>)}
          <SourceLinks items={finding.evidence} onSelect={onEvidence}/></details>)}
        <p>{text(output.conclusion)}</p>
      </article>)}
      {!list(data.outputs).length && <p>Измеренного влияния недостаточно для ранжирования. Это не означает отсутствия существенных проблем.</p>}
      {list(data.unquantified_findings).length>0 && <div className="unknown-effects"><h3>Влияние не установлено</h3><p>Эти замечания не имеют числового ранга и не считаются малозначимыми.</p>{data.unquantified_findings.map((item,i)=><article key={item.code || i}><h4>{text(item.title)}</h4><p>{text(item.reason)}</p><p>{text(item.action)}</p><SourceLinks items={item.evidence} onSelect={onEvidence}/></article>)}</div>}
    </>}
    <details><summary>Границы оценки</summary><p>{text(data.policy?.meaning)}</p><ul>{list(data.limitations).map((item,i)=><li key={i}>{text(item)}</li>)}</ul></details>
  </section>;
}
