import React from 'react';

export default function SourceLinks({items, onSelect, maxVisible}) {
  const refs = Array.isArray(items) ? items.filter(e => e && typeof e.sheet === 'string' && typeof e.cell === 'string') : [];
  if (!refs.length) return null;
  const button=(e,i)=><button type="button" key={`${e.sheet}-${e.cell}-${i}`} onClick={() => onSelect?.(e)}>{e.sheet}!{e.cell}</button>;
  const limit=Number.isInteger(maxVisible) && maxVisible>0 ? maxVisible : refs.length;
  return <div className="source-links"><div className="evidence"><span>Источники</span>{refs.slice(0,limit).map(button)}</div>{refs.length>limit && <details className="source-more"><summary>Ещё источники · {refs.length-limit}</summary><div className="evidence">{refs.slice(limit).map(button)}</div></details>}</div>;
}
