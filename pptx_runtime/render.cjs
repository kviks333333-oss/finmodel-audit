'use strict';

// The renderer accepts data only. It never evaluates model code or loads media,
// URLs or paths from the report. The parent owns time/resource limits and files.
const PptxGenJS = require('pptxgenjs');
const JSZip = require('jszip');
const MAX_BYTES = 32 * 1024 * 1024;
const EMU = 914400;
const STYLE = Object.freeze({font: 'Arial', body: 10, title: 12,
  width: 12192000 / EMU, height: 6858000 / EMU,
  accent: '408D7A', dark: '176E59', light: 'E6F0EC', ink: '202925'});

function text(value, limit = 1000000) {
  if (typeof value !== 'string' || value.length > limit) throw new Error('Invalid text');
  return value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
}
function list(value, limit) {
  if (!Array.isArray(value) || value.length > limit) throw new Error('Invalid list');
  return value;
}
function paragraphHeight(value) {
  return Math.max(.3, Math.ceil([...value].length / 165) * .2 + .08);
}
function rowHeight(values, widths) {
  const lines = Math.max(...values.map((value, index) =>
    Math.max(1, Math.ceil([...value].length / Math.max(8, Math.trunc(widths[index] * 12))))
      + (value.match(/\n/g) || []).length));
  return Math.max(.34, lines * .16 + .13);
}
function textOptions(x, y, w, h, overrides = {}) {
  return {x, y, w, h, fontFace: STYLE.font, fontSize: STYLE.body,
    color: STYLE.ink, margin: 0, breakLine: false, align: 'left', valign: 'top',
    lang: 'ru-RU', paraSpaceAfter: 3, lineSpacingMultiple: 1.1,
    fit: 'none', ...overrides};
}
function addTable(slide, source, y) {
  const headers = list(source.headers, 12).map(value => text(value, 2000));
  if (!headers.length) throw new Error('Empty table');
  const widths = list(source.widths, 12).map(value => {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < .1 || value > 12.4) throw new Error('Invalid width');
    return value;
  });
  if (widths.length !== headers.length) throw new Error('Invalid columns');
  const width = widths.reduce((a, b) => a + b, 0);
  if (width > 12.4 + 1e-8) throw new Error('Table too wide');
  const values = [headers, ...list(source.rows, 100).map(row => {
    const cells = list(row, 12).map(value => text(value, 10000));
    if (cells.length !== headers.length) throw new Error('Invalid row');
    return cells;
  })];
  const heights = values.map(row => rowHeight(row, widths));
  const height = heights.reduce((a, b) => a + b, 0);
  if (y + height > 6.88 + 1e-8) throw new Error('Table exceeds slide');
  const rows = values.map((row, index) => row.map(value => ({text: value, options: {
    fontFace: STYLE.font, fontSize: STYLE.body, bold: index === 0,
    color: index === 0 ? 'FFFFFF' : STYLE.ink,
    fill: {color: index === 0 ? STYLE.accent : index % 2 === 0 ? STYLE.light : 'FFFFFF'},
    valign: 'middle', margin: [3.54, 5.12, 2.36, 5.12],
    border: [{type: 'none'}, {type: 'none'}, {type: 'solid', pt: .5, color: 'B8DBD6'}, {type: 'none'}]
  }})));
  slide.addTable(rows, {x: .45, y, w: width, colW: [...widths], rowH: [...heights],
    fontFace: STYLE.font, fontSize: STYLE.body, color: STYLE.ink,
    margin: [3.54, 5.12, 2.36, 5.12], paraSpaceAfter: 3,
    align: 'left', valign: 'middle', autoPage: false, verbose: false,
    objectName: 'Editable financial table'});
}
function addChart(presentation, slide, source, y) {
  const categories = list(source.categories, 8).map(value => text(value, 1000));
  const values = list(source.values, 8).map(value => {
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('Invalid chart value');
    return value;
  });
  if (!values.length || values.length !== categories.length) throw new Error('Invalid chart');
  const unit = text(source.unit || 'Значение', 500);
  slide.addChart(presentation.ChartType.bar, [{name: unit, labels: categories, values}], {
    x: .65, y, w: 12, h: 2.75, barDir: 'col', catAxisLabelsPos: 'low',
    barGrouping: 'clustered', barGapWidthPct: 100, showTitle: false, showLegend: false,
    showValue: false, showCatName: false, showSerName: false,
    fontFace: STYLE.font, fontSize: STYLE.body, lang: 'ru-RU',
    catAxisLabelFontFace: STYLE.font, catAxisLabelFontSize: STYLE.body,
    valAxisLabelFontFace: STYLE.font, valAxisLabelFontSize: STYLE.body,
    legendFontFace: STYLE.font, legendFontSize: STYLE.body,
    dataLabelFormatCode: '#,##0.##', dataLabelColor: STYLE.ink,
    dataLabelBkgrdColor: 'FFFFFF', dataLabelPosition: 'outEnd',
    dataLabelFontFace: STYLE.font, dataLabelFontSize: STYLE.body,
    catAxisLabelColor: '747A77', valAxisLabelColor: '747A77',
    valAxisLabelFormatCode: '#,##0',
    catGridLine: {style: 'none'}, valGridLine: {color: 'E6F0EC', size: .5},
    chartArea: {fill: {color: 'FFFFFF'}, roundedCorners: false},
    plotArea: {fill: {color: 'FFFFFF'}},
    chartColors: values.map((_, index) => index === 0 ? '747A77' : STYLE.accent),
    showBorder: false, objectName: 'Editable conditional data chart'
  });
}
function singleLevelChartCategories(xml) {
  // PptxGenJS 4.0.1 writes multiLvlStrRef even for our flat string[] labels.
  // Express that one level as standard strRef/strCache for portable readers.
  // Copy the formula, count and point XML unchanged; never flatten real levels.
  const normalized = xml.replace(/<c:cat>([\s\S]*?)<\/c:cat>/g, (category, body) => {
    if (!/<c:multiLvlStrRef\b/.test(body)) return category;
    const match = body.match(/^\s*<c:multiLvlStrRef>\s*(<c:f>[^<]*<\/c:f>)\s*<c:multiLvlStrCache>\s*(<c:ptCount val="(\d+)"\s*\/>)\s*<c:lvl>([\s\S]*?)<\/c:lvl>\s*<\/c:multiLvlStrCache>\s*<\/c:multiLvlStrRef>\s*$/);
    if (!match || (body.match(/<c:lvl\b/g) || []).length !== 1) throw new Error('Unsupported category levels');
    const [, formula, pointCount, countText, level] = match;
    const count = Number(countText);
    const pointPattern = /<c:pt idx="(\d+)"><c:v>[^<]*<\/c:v><\/c:pt>/g;
    const points = [...level.matchAll(pointPattern)];
    if (count < 1 || count > 8 || points.length !== count
        || points.some((point, index) => Number(point[1]) !== index)
        || level.replace(pointPattern, '').trim()) throw new Error('Unsupported category cache');
    return '<c:cat><c:strRef>' + formula + '<c:strCache>' + pointCount + level + '</c:strCache></c:strRef></c:cat>';
  });
  if (/<c:multiLvlStrRef\b/.test(normalized)) throw new Error('Unsupported category reference');
  return normalized;
}
async function render(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) throw new Error('Invalid plan');
  const slides = list(plan.slides, 24);
  if (!slides.length) throw new Error('Empty plan');
  const presentation = new PptxGenJS();
  presentation.defineLayout({name: 'FINMODEL', width: STYLE.width, height: STYLE.height});
  presentation.layout = 'FINMODEL';
  presentation.author = 'Financial model audit';
  presentation.subject = 'Сохранённые результаты анализа финансовой модели';
  presentation.title = 'Анализ финансовой модели';
  presentation.company = '';
  presentation.lang = 'ru-RU';
  presentation.theme = {headFontFace: STYLE.font, bodyFontFace: STYLE.font};
  for (const [index, source] of slides.entries()) {
    if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error('Invalid slide');
    const slide = presentation.addSlide();
    slide.background = {color: 'FFFFFF'};
    slide.addText(text(source.title, 500), textOptions(.45, .36, 12.4, .38,
      {fontSize: STYLE.title, bold: true, color: STYLE.dark, objectName: 'Title'}));
    let y = .98;
    for (const paragraph of list(source.paragraphs, 30)) {
      const value = text(paragraph, 3000), height = paragraphHeight(value);
      slide.addText(value, textOptions(.45, y, 12.4, height, {objectName: 'Statement'}));
      y += height + .12;
    }
    if (source.chart) {addChart(presentation, slide, source.chart, y); y += 2.88;}
    if (source.table) addTable(slide, source.table, y);
    else if (y > 6.8) throw new Error('Text exceeds slide');
    const sources = list(source.sources, 1000).map(value => text(value, 2000));
    let footer = sources.join('; ');
    if ([...footer].length > 155) footer = [...footer].slice(0, 109).join('').trimEnd() + '… Полные источники в заметках.';
    footer = (footer ? footer + '   ' : '') + `${index + 1}/${slides.length}`;
    slide.addText(footer, textOptions(.45, 7.02, 12.4, .3,
      {color: '747A77', objectName: 'Sources and page'}));
    slide.addNotes('Источники: ' + sources.join('; ') + '\n' + text(source.note || ''));
  }
  if (presentation._slides.length !== slides.length) throw new Error('Unexpected pagination');
  const result = await presentation.write({outputType: 'nodebuffer', compression: true});
  // PptxGenJS exposes notes content but not the notes-master font size. Keep
  // its package/relationships unchanged; only set this documented style value.
  const zip = await JSZip.loadAsync(result);
  const notesMaster = zip.file('ppt/notesMasters/notesMaster1.xml');
  if (!notesMaster) throw new Error('Notes master missing');
  const masterXml = await notesMaster.async('string');
  zip.file('ppt/notesMasters/notesMaster1.xml', masterXml.replace(/(<a:defRPr\b[^>]*\bsz=")1200("[^>]*>)/g,
    (_, prefix, suffix) => prefix + '1000' + suffix));
  // PptxGenJS 4.0.1 can declare slideMaster2..N content types although it
  // writes only slideMaster1. Remove only declarations for absent ZIP parts;
  // real part bytes, relationships and the library's element order stay intact.
  const contentTypes = zip.file('[Content_Types].xml');
  if (!contentTypes) throw new Error('Content types missing');
  const contentXml = await contentTypes.async('string');
  const filteredContentXml = contentXml.replace(/<Override(?=\s)(?:[^>"']|"[^"]*"|'[^']*')*\/>/g, tag => {
    const attributes = [...tag.matchAll(/\s([A-Za-z_][\w.:-]*)\s*=\s*(["'])(.*?)\2/g)];
    const names = attributes.filter(match => match[1] === 'PartName');
    if (names.length !== 1) throw new Error('Invalid content type part');
    const partName = names[0][3];
    // Library part names are canonical ASCII paths. Reject unusual names
    // rather than guessing about XML entities or URI decoding and deleting one.
    if (!/^\/[A-Za-z0-9_.\/-]+$/.test(partName) || partName.split('/').some(segment => segment === '..' || segment === '.')) {
      throw new Error('Unsupported content type part');
    }
    return zip.file(partName.slice(1)) ? tag : '';
  });
  zip.file('[Content_Types].xml', filteredContentXml);
  for (const part of Object.values(zip.files)) {
    if (!part.dir && /^ppt\/charts\/chart[0-9]+\.xml$/.test(part.name)) {
      zip.file(part.name, singleLevelChartCategories(await part.async('string')));
    }
  }
  const output = await zip.generateAsync({type: 'nodebuffer', compression: 'DEFLATE'});
  if (!output.length || output.length > MAX_BYTES) throw new Error('Output exceeds limit');
  return output;
}
async function main() {
  const chunks = []; let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_BYTES) throw new Error('Input exceeds limit');
    chunks.push(chunk);
  }
  const plan = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const output = await render(plan);
  await new Promise((resolve, reject) => process.stdout.write(output, error => error ? reject(error) : resolve()));
}
// Library warnings must not corrupt the binary stream or expose report text.
console.log = console.info = console.warn = console.error = () => {};
if (require.main === module) main().catch(() => {process.stderr.write('PowerPoint rendering failed.\n'); process.exitCode = 2;});
module.exports = {render, singleLevelChartCategories};
