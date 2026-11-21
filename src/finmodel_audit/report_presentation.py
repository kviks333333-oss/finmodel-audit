"""Offline HTML fragments for the shared semantic presentation view.

Only escaped text and static SVG are emitted. Charts show saved endpoints,
never an invented time series; every plotted value has a full data table.
"""
from html import escape
import math
from .presentation import format_saved_value


def text(value):
    return escape(str(value)) if value is not None else "Не установлено"


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def sources(items):
    refs = [(x["sheet"], x["cell"]) for x in items if isinstance(x, dict)
            and isinstance(x.get("sheet"), str) and isinstance(x.get("cell"), str)] if isinstance(items, list) else []
    return "<p class='source-links'>" + " · ".join(text(f"{sheet}!{cell}") for sheet, cell in dict.fromkeys(refs)) + "</p>" if refs else ""


def overview(view):
    data = view.get("overview") if isinstance(view.get("overview"), dict) else {}
    summaries = data.get("summary") if isinstance(data.get("summary"), list) else []
    steps = data.get("next_steps") if isinstance(data.get("next_steps"), list) else []
    parts = ["<section id='overview' class='overview'><p class='eyebrow'>ГЛАВНОЕ</p><h2>" + text(data.get("title") or "Результаты анализа") + "</h2>"]
    parts.extend("<p class='overview-line'>" + text(line) + "</p>" for line in summaries if isinstance(line, str))
    if not summaries:
        parts.append("<p>Доступные результаты приведены по разделам ниже. Пригодность модели для принятия решений не установлена.</p>")
    if steps:
        parts.append("<div class='next-steps'><h3>Что проверить дальше</h3><ol>")
        for step in steps:
            if isinstance(step, dict):
                evidence = sources(step.get("evidence"))
                parts.append("<li>" + text(step.get("text")) +
                             ("<details><summary>Источники для проверки</summary>" + evidence + "</details>" if evidence else "") + "</li>")
        parts.append("</ol></div>")
    notices = view.get("notices") if isinstance(view.get("notices"), list) else []
    if notices:
        parts.append("<ul class='presentation-notices'>" + "".join("<li>" + text(n) + "</li>" for n in notices if isinstance(n, str)) + "</ul>")
    return "".join(parts) + "</section>"


def metrics(view):
    items = view.get("metrics") if isinstance(view.get("metrics"), list) else []
    if not items:
        return ""
    parts = ["<section id='key-metrics'><p class='eyebrow'>СОХРАНЁННЫЕ ДАННЫЕ</p><h2>Ключевые показатели</h2><div class='metric-grid'>"]
    for item in items:
        if not isinstance(item, dict):
            continue
        period_label = item.get("period_label", item.get("period"))
        period_text = "Период: " + text(period_label) if item.get("period") is not None else "Период не установлен"
        binding = "Контекст связан" if item.get("binding_status") == "bound" else "Контекст связан частично"
        parts.append("<article class='metric'><h3>" + text(item.get("label")) + "</h3><p class='metric-number'>" +
                     text(item.get("value_label", item.get("value"))) + "</p><p>Подпись единицы: " + text(item.get("unit")) +
                     "</p><p>" + period_text + "</p><p class='muted'>" +
                     text(item.get("period_type_label") or "Факт/прогноз не определён") + "</p><p class='muted'>" + binding + "</p>" + sources(item.get("evidence")) + "</article>")
    return "".join(parts) + "</div><p class='fine-print'>Для краткого обзора числа округлены; точные значения приведены в таблицах. Значения и исходные подписи единиц показаны раздельно. Масштабирование и Excel-форматирование не применяются.</p></section>"


def _bars(points, title, chart_id):
    """A zero-based endpoint bar chart; normalize to avoid huge-domain overflow."""
    if not points or not all(finite(p.get("value")) for p in points):
        return "<p class='chart-unavailable'>График не построен: не для всех точек есть конечные числовые значения. Данные сохранены в таблице.</p>"
    if len(points) > 20:
        return "<p class='chart-unavailable'>Для читаемости большой набор представлен полной таблицей.</p>"
    magnitude = max(abs(p["value"]) for p in points) or 1
    values = [p["value"] / magnitude for p in points]
    lo, hi = min(0, *values), max(0, *values)
    if hi == lo:
        hi = 1
    top, bottom, left, right = 44, 230, 52, 668
    def y(value):
        return bottom - (value - lo) / (hi - lo) * (bottom - top)
    zero, spacing = y(0), (right - left) / len(points)
    width = min(130, spacing * .55)
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 720 296' role='img' aria-labelledby='{chart_id}-title {chart_id}-description' class='static-chart'>",
             f"<title id='{chart_id}-title'>" + text(title) + "</title>",
             f"<desc id='{chart_id}-description'>Столбцы показанных значений от нулевой линии. Точные значения и источники приведены в таблице под графиком. Промежуточные значения не восстанавливаются.</desc>",
             f"<line x1='{left}' x2='{right}' y1='{zero:.3f}' y2='{zero:.3f}' stroke='#9aa8ad' stroke-width='1'/>",
             f"<text x='42' y='{zero+4:.3f}' text-anchor='end' class='axis-label'>0</text>"]
    for i, (point, value) in enumerate(zip(points, values)):
        x, ordinate = left + spacing * (i+.5), y(value)
        height = abs(zero - ordinate)
        fill = "#8198a2" if i == 0 else "#285c57"
        parts.append(f"<rect x='{x-width/2:.3f}' y='{min(zero,ordinate):.3f}' width='{width:.3f}' height='{height:.3f}' rx='3' fill='{fill}'/>")
        label_y = ordinate - 10 if value >= 0 else ordinate + 18
        parts.append(f"<text x='{x:.3f}' y='{label_y:.3f}' text-anchor='middle' class='chart-value'>" + text(f"{point['value']:.6g}") + "</text>")
        # The full label is in the table and SVG title. Long untrusted labels
        # never create clipped, misleading chart annotations.
        label = str(point.get("label") or "Не установлен")
        shown = label if len(label) <= 28 else f"Точка {i+1}"
        parts.append(f"<text x='{x:.3f}' y='264' text-anchor='middle' class='axis-label'>" + text(shown) + "</text>")
    return "".join(parts) + "</svg>"


def chart(data, index, sensitivity=False):
    title = data.get("title") or "Сравнение сохранённых значений"
    points = [x for x in data.get("points", []) if isinstance(x, dict)] if isinstance(data.get("points"), list) else []
    if sensitivity:
        points = [{"label": "База", "value": data.get("baseline"), "evidence": [data.get("target")], "delta": 0}] + points
    comparisons = [x for x in data.get("comparisons", []) if isinstance(x, dict)] if isinstance(data.get("comparisons"), list) else []
    comparison_labels = {"total_growth": "Общий относительный рост", "cagr": "CAGR", "difference": "Разница сохранённых значений"}
    chart_id = f"{'sensitivity' if sensitivity else 'comparison'}-chart-{index}"
    parts = [f"<figure class='chart-figure' id='{chart_id}'><figcaption><h3>" + text(title) + "</h3><p>Подпись единицы: " + text(data.get("unit")) + "</p></figcaption>",
             _bars(points, title, chart_id)]
    if data.get("note"):
        parts.append("<p class='chart-note'>" + text(data["note"]) + "</p>")
    if not sensitivity and not data.get("note"):
        parts.append("<p class='chart-note'>Показаны только выбранные конечные точки. Промежуточная траектория не восстановлена.</p>")
    parts.append("<details class='chart-data'><summary>Точные данные и источники графика</summary><div class='table-scroll'><table><caption>Данные графика: " + text(title) + "</caption><thead><tr><th scope='col'>Точка</th><th scope='col'>Значение</th>")
    parts.append("<th scope='col'>Изменение к базе</th>" if sensitivity else "<th scope='col'>Тип периода</th>")
    parts.append("<th scope='col'>Источники</th></tr></thead><tbody>")
    for point in points:
        evidence = point.get("evidence") if sensitivity else [{"sheet": point.get("sheet"), "cell": point.get("cell")}]
        parts.append("<tr><th scope='row'>" + text(point.get("label")) + "</th><td>" + text(point.get("value")) + "</td><td>" +
                     text(point.get("delta") if sensitivity else point.get("period_type_label") or "Факт/прогноз не определён") + "</td><td>" + sources(evidence) + "</td></tr>")
    parts.append("</tbody></table></div>")
    if comparisons and not sensitivity:
        parts.append("<div class='table-scroll'><table><caption>Точные результаты сравнений</caption><thead><tr><th scope='col'>Расчёт</th><th scope='col'>Значение</th><th scope='col'>Единица</th><th scope='col'>Формула</th></tr></thead><tbody>")
        for item in comparisons:
            display = item.get("display") if isinstance(item.get("display"), dict) else {}
            parts.append("<tr><th scope='row'>" + comparison_labels.get(item.get("kind"), "Сравнение") + "</th><td>" +
                         text(display.get("value")) + "</td><td>" + text(display.get("unit")) + "</td><td>" + text(item.get("formula")) + "</td></tr>")
        parts.append("</tbody></table></div>")
    parts.append("</details>")
    if not sensitivity:
        if comparisons:
            parts.append("<ul class='comparison-highlights'>")
            for item in comparisons:
                display = item.get("display") if isinstance(item.get("display"), dict) else {}
                label = comparison_labels.get(item.get("kind"), "Сравнение")
                parts.append("<li>" + label + ": " + text(format_saved_value(display.get("value"))) + " " + text(display.get("unit")) + "</li>")
            parts.append("</ul>")
    return "".join(parts) + sources(data.get("evidence")) + "</figure>"


def financial_groups(view):
    groups = view.get("financial_groups") if isinstance(view.get("financial_groups"), list) else []
    if not groups:
        return ""
    parts = ["<section id='financial-findings'><p class='eyebrow'>СОДЕРЖАТЕЛЬНЫЕ ВОПРОСЫ</p><h2>Финансовые проверки</h2><p class='fine-print'>Находки объединены по типу вопроса. Число повторений не является оценкой финансовой существенности.</p>"]
    for group in groups:
        if not isinstance(group, dict):
            continue
        label = "Требует проверки" if group.get("status") == "candidate" else "Наблюдение"
        parts.append("<article class='finding-group'><p class='eyebrow'>" + label + " · случаев: " + text(group.get("count")) + "</p><h3>" + text(group.get("title")) + "</h3>")
        for field, heading in (("detail", ""), ("consequence", "Что это означает"), ("action", "Следующий шаг")):
            if group.get(field):
                parts.append(("<h4>" + heading + "</h4>" if heading else "") + "<p>" + text(group[field]) + "</p>")
        parts.append("<p class='fine-print'>Финансовая существенность не оценена.</p><details><summary>Источники группы</summary>" + sources(group.get("evidence")) + "</details></article>")
    return "".join(parts) + "</section>"


CSS = """
:root{color-scheme:light}*{box-sizing:border-box}body{font:15px/1.65 Arial,Helvetica,sans-serif;color:#263b44;background:#f5f6f3;margin:0}main{max-width:1120px;margin:0 auto;background:#fff;padding:48px 56px 32px}h1{font-size:32px;line-height:1.2;overflow-wrap:anywhere;letter-spacing:-.8px;margin:10px 0 18px}h2{font-size:25px;line-height:1.3;letter-spacing:-.4px;margin:0 0 18px}h3{font-size:17px;line-height:1.4;margin:0 0 9px}h4{font-size:12px;margin:17px 0 4px}p{margin:9px 0;white-space:pre-wrap;overflow-wrap:anywhere}.eyebrow,.overline{color:#58746d;font-size:10px;letter-spacing:1.6px;font-weight:700}.muted,.fine-print,.meta{color:#65747a;font-size:12px}.report-title{border-bottom:1px solid #ccd8d3;padding-bottom:24px}.report-title .meta{margin-top:20px}.section-nav{display:flex;gap:16px;flex-wrap:wrap;margin:22px 0 0;font-size:12px}.section-nav a{color:#285c57;text-decoration:none;border-bottom:1px solid #b4ccc5}section{margin:34px 0 0;padding:30px 0 0;border-top:1px solid #dce3df}.overview{border:0;margin-top:0}.overview-line{font-size:17px;line-height:1.8}.overview .next-steps{margin-top:24px}.next-steps li{padding:5px 0}.presentation-notices{color:#6c6854;font-size:12px;padding-left:20px}.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.metric{border:1px solid #dce5df;border-top:3px solid #52756c;border-radius:5px;padding:17px}.metric h3{font-size:13px}.metric .metric-number{font-size:27px;font-weight:700;letter-spacing:-.5px;line-height:1.3;margin:14px 0}.metric p{font-size:11px;margin:4px 0}.source-links,.evidence{color:#527568;font-size:11px;overflow-wrap:anywhere}.section-top{display:flex;align-items:center;justify-content:space-between;gap:15px;font-size:11px;margin-bottom:12px}.stage-number{color:#6a8780}.status{font-size:11px;color:#706845;background:#f4f0df;padding:4px 9px;border-radius:4px}.stage-summary{font-size:15px;line-height:1.8}.stage-observation{padding:20px 0;border-bottom:1px solid #e3e9e5}.stage-observation .evidence{margin-top:15px}.stage-observation small{color:#687d70}.impact{color:#687674;font-size:12px}.limits{padding:5px 0 5px 15px;border-left:2px solid #b6a675;margin:22px 0;color:#716e60;font-size:12px}.limits h3{font-size:13px}.limits ul{margin:6px 0;padding-left:18px}.finding-group{padding:23px 0;border-top:1px solid #dce5df}.finding-group h3{font-size:18px}.finding-group p{font-size:13px}details{margin:14px 0}summary{cursor:pointer;font-size:12px;color:#456b60;font-weight:600}details[open]>summary{margin-bottom:12px}.technical-appendix{margin-top:38px;border-top:2px solid #6d8b80;padding-top:20px}.technical-appendix>summary{font-size:17px}.technical-appendix section{margin-top:22px}.technical-appendix article{padding:14px 0;border-bottom:1px solid #e5eae6}.technical-appendix h2{font-size:20px}.technical-appendix h3{font-size:14px}.technical-appendix p,.technical-appendix li{font-size:12px}table{width:100%;border-collapse:collapse;font-size:12px;margin:14px 0}th,td{text-align:left;vertical-align:top;padding:10px 11px;border-bottom:1px solid #dce4df;overflow-wrap:anywhere}th{background:#f1f5f2;color:#425a50;font-weight:600}caption{text-align:left;font-size:12px;font-weight:600;margin:7px 0}.table-scroll{overflow-x:auto}.fact-table{table-layout:fixed}.fact-ref,.fact-sheet,.fact-period-type{display:block;font-size:10px;overflow-wrap:anywhere;margin-top:4px;color:#55756b}.fact-note,.fact-issues{font-size:11px;color:#64746b}.fact-issues{padding-left:16px}.fact-panel{margin:24px 0}.fact-panel h3{font-size:15px}.fact-panel code,pre{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f3;padding:10px;border-radius:4px}.chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.chart-figure{margin:20px 0;border:1px solid #dce5df;border-radius:6px;padding:18px;min-width:0}.chart-figure h3{font-size:15px}.chart-figure figcaption p,.chart-note{font-size:11px;color:#64746b}.static-chart{display:block;width:100%;height:auto;margin:10px auto}.static-chart .axis-label{font:13px Arial,sans-serif;fill:#60736d}.static-chart .chart-value{font:14px Arial,sans-serif;fill:#263b44}.chart-data{font-size:11px}.chart-data table{font-size:10px}.comparison-highlights{font-size:12px;padding-left:18px}.chart-unavailable{font-size:12px;color:#786c4e}.completion-note{padding:12px 16px;background:#f7f0df;border-left:3px solid #b3954e}.check-panel{margin-top:20px}.check-panel .section-top{gap:12px}.check-calculation{font-size:12px}.sensitivity-scenario{padding:18px 0}.report-footer{font-size:10px;color:#6c7c73;border-top:1px solid #dce3df;margin-top:36px;padding-top:18px;overflow-wrap:anywhere}.report-footer p{margin:4px 0}.full-stage-observations{margin-top:22px}
@media(max-width:760px){main{padding:28px 20px}h1{font-size:26px}h2{font-size:22px}.metric-grid,.chart-grid{grid-template-columns:1fr}.fact-table{table-layout:auto}.section-nav{gap:12px}.metric .metric-number{font-size:24px}}
.print-option{width:16px;height:16px;accent-color:#285c57;vertical-align:middle;margin:24px 8px 4px 0}.print-option-label{font-size:13px;color:#285c57;cursor:pointer;font-weight:600}.print-option:focus-visible{outline:2px solid #285c57;outline-offset:3px}.print-help{font-size:11px;color:#65747a;max-width:760px;margin:4px 0 18px}.print-summary-note{display:none}
@page{size:A4;margin:16mm}
@media print{body{background:#fff;font-size:10pt}main{max-width:none;margin:0;padding:0}.section-nav,.print-option,.print-option-label,.print-help{display:none!important}.print-summary-note{display:block;font-size:9pt;color:#65747a}.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.chart-grid{grid-template-columns:1fr 1fr}h1{font-size:24pt}h2{font-size:18pt}h2,h3,h4,summary{break-after:avoid}.metric,.chart-figure{break-inside:avoid}article{break-inside:auto}.table-scroll{overflow:visible}thead{display:table-header-group}tr{break-inside:avoid}details,details[open]{display:block!important}details:not([open])>:not(summary){display:block!important}details::details-content{display:block!important;content-visibility:visible!important}details>summary{list-style:none;color:#263b44}details>summary::marker{content:''}#print-full-appendix~.technical-appendix{display:none!important;break-before:page}#print-full-appendix:checked~.technical-appendix{display:block!important}#print-full-appendix:checked~.print-summary-note{display:none!important}.static-chart{max-height:75mm}.source-links,.evidence{color:#3b5a4d}.fine-print,.meta{color:#53635b}.chart-data,.chart-note{display:block!important}}
"""
