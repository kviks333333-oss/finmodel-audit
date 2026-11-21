import React from 'react';
import {render,screen,fireEvent,cleanup} from '@testing-library/react';
import {afterEach,expect,test,vi} from 'vitest';
import DecisionPanel from './DecisionPanel.jsx';
import PptxDownload from './PptxDownload.jsx';

afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals();});
const data={outputs:[{target:{sheet:'DCF',cell:'C39'},label:'NPV',raw_unit:'тыс. руб.',baseline:100,ranked_findings:[
  {rank:1,code:'overlap',title:'Состав FCFF',max_absolute_delta:60,max_absolute_relative_delta:.6,reason:'Два независимых варианта',action:'Проверить состав',alternatives:[{title:'Без Net Profit',result:40,delta:-60},{title:'Без NOPAT',result:45,delta:-55}],evidence:[{sheet:'DCF',cell:'C39'}]}]}],
  unquantified_findings:[{title:'Входы IRR',reason:'Влияние неизвестно',action:'Проверить потоки',evidence:[]}],policy:{meaning:'Без нормативных порогов'},limitations:[],
  suitability:{summary:'До использования NPV нужна проверка FCFF.',supported_uses:['Чтение исходных значений'],unsubstantiated_uses:['Выбор проекта по IRR'],required_checks:['Проверить потоки']}};

test('one issue keeps two alternatives and unknown impact separate, source opens',()=>{
 const onEvidence=vi.fn();render(<DecisionPanel data={data} onEvidence={onEvidence}/>);
 expect(screen.getByRole('cell',{name:'60 тыс. руб.'})).toBeTruthy();
 expect(screen.getByRole('cell',{name:'60%'})).toBeTruthy();
 expect(screen.getByRole('heading',{name:'Влияние не установлено'})).toBeTruthy();
 expect(screen.queryByText('115')).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:/DCF!C39/}));expect(onEvidence).toHaveBeenCalledWith({sheet:'DCF',cell:'C39'});
});
test('zero baseline does not acquire a percentage or hide unknowns',()=>{
 const zero=structuredClone(data);zero.outputs[0].baseline=0;zero.outputs[0].ranked_findings[0].max_absolute_relative_delta=null;
 render(<DecisionPanel data={zero} onEvidence={()=>{}}/>);
 expect(screen.getByText('Не определяется при нулевой базе')).toBeTruthy();expect(screen.queryByText('Infinity')).toBeNull();
});
test('conclusion includes supported use, restriction and concrete check',()=>{
 render(<DecisionPanel data={data} onEvidence={()=>{}} conclusionOnly/>);
 for (const value of ['Чтение исходных значений','Выбор проекта по IRR','Проверить потоки']) expect(screen.getByText(value)).toBeTruthy();
});
test('finite ratio remains a ratio when percent conversion would overflow',()=>{
 const extreme=structuredClone(data);extreme.outputs[0].ranked_findings[0].max_absolute_relative_delta=1e308;
 const {container}=render(<DecisionPanel data={extreme} onEvidence={()=>{}}/>);
 expect(container.textContent).toContain('(доля)');expect(container.textContent).not.toContain('Не установлено%');expect(container.textContent).not.toContain('Infinity');
});
test('PowerPoint errors leave UI available for retry',async()=>{
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,json:async()=>({detail:'Презентация не готова. Анализ сохранён.'})}));
 render(<PptxDownload jobId="job"/>);fireEvent.click(screen.getByRole('button',{name:'Скачать PowerPoint'}));
 expect(await screen.findByRole('alert')).toHaveProperty('textContent','Презентация не готова. Анализ сохранён.');
 expect(screen.getByRole('button',{name:'Скачать PowerPoint'}).disabled).toBe(false);
});
test('PowerPoint success downloads blob without another analysis request',async()=>{
 const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
 vi.stubGlobal('URL',{createObjectURL:vi.fn(()=> 'blob:deck'),revokeObjectURL:vi.fn()});
 const fetch=vi.fn().mockResolvedValue({ok:true,headers:{get:()=> 'application/vnd.openxmlformats-officedocument.presentationml.presentation'},blob:async()=>new Blob(['PK'])});vi.stubGlobal('fetch',fetch);
 render(<PptxDownload jobId="job"/>);fireEvent.click(screen.getByRole('button',{name:'Скачать PowerPoint'}));
 await vi.waitFor(()=>expect(click).toHaveBeenCalledOnce());
 expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/jobs/job/download?format=pptx');
});
