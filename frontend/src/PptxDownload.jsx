import React,{useState} from 'react';

export default function PptxDownload({jobId}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  async function download() {
    setBusy(true);setError('');
    try {
      const response=await fetch(`/api/jobs/${encodeURIComponent(jobId)}/download?format=pptx`);
      if (!response.ok) {
        let data;try{data=await response.json();}catch{}
        throw new Error(typeof data?.detail==='string'?data.detail:'Не удалось скачать презентацию. Анализ сохранён.');
      }
      if (!response.headers.get('content-type')?.includes('application/vnd.openxmlformats-officedocument.presentationml.presentation')) throw new Error('Сервис вернул неожиданный формат презентации.');
      const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download=`analysis-${jobId}.pptx`;document.body.appendChild(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch (error) {setError(error.message || 'Не удалось скачать презентацию.');}
    finally {setBusy(false);}
  }
  return <span className="pptx-download"><button className="secondary" disabled={busy} onClick={download}>{busy?'Готовим PowerPoint…':'Скачать PowerPoint'}</button>{error && <span role="alert" className="error">{error}</span>}</span>;
}
