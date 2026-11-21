import React from 'react';
import {Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis} from 'recharts';

export const finite = value => typeof value === 'number' && Number.isFinite(value);
export function readableNumber(value) {
  if (!finite(value)) return 'Не установлено';
  if (value !== 0 && Math.abs(value) < .0001) return String(value);
  return new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 4}).format(value);
}

export function chartDomain(values) {
  const valid = values.filter(finite);
  const min = Math.min(0, ...valid), max = Math.max(0, ...valid);
  if (min === max) return [-1, 1];
  const padding = (max - min) * .08;
  return [min < 0 ? min - padding : 0, max > 0 ? max + padding : 0];
}

// The visible source table is the accessible, exact counterpart of this graphic.
// No curve or intermediate period is synthesized from endpoint data.
export function ValueBars({points, valueKey = 'value', label = 'Сохранённое значение'}) {
  const rows = points.filter(point => finite(point[valueKey]));
  if (!rows.length) return null;
  return <div className="report-chart" aria-hidden="true">
    <ResponsiveContainer width="100%" height={230} initialDimension={{width: 640, height: 230}}>
      <BarChart data={rows} margin={{top: 10, right: 12, bottom: 8, left: 8}} accessibilityLayer={false}>
        <CartesianGrid vertical={false} stroke="#e6ece8"/>
        <XAxis dataKey="label" tick={{fontSize: 12, fill: '#56685f'}} axisLine={false} tickLine={false}/>
        <YAxis width={78} domain={chartDomain(rows.map(point => point[valueKey]))} tickFormatter={readableNumber} tick={{fontSize: 11, fill: '#697a70'}} axisLine={false} tickLine={false}/>
        <ReferenceLine y={0} stroke="#879b8e"/>
        <Tooltip formatter={value => [readableNumber(value), label]} cursor={{fill: '#eef3ef'}}/>
        <Bar dataKey={valueKey} fill="#547f70" maxBarSize={64} radius={[4, 4, 0, 0]} isAnimationActive={false}/>
      </BarChart>
    </ResponsiveContainer>
  </div>;
}
