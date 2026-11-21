"""Escaped readable priorities and conditional suitability derived from evidence."""
from html import escape
import math
from .presentation import format_saved_value


def decision_html(data, conclusion_only=False):
    def text(value):
        return escape(str(value)) if value is not None else ""
    def lines(values):
        return "<ul>" + "".join("<li>" + text(x) + "</li>" for x in values) + "</ul>"
    def refs(values):
        return "<p class='evidence'>Источники: " + ", ".join(text(x['sheet']+"!"+x['cell']) for x in values) + "</p>"
    title = "Применение модели и необходимые проверки" if conclusion_only else "Приоритет проверки замечаний"
    parts = ["<section id='" + ("model-suitability" if conclusion_only else "review-priorities") + "'><h2>" + title + "</h2>"]
    if conclusion_only:
        suitability = data["suitability"]
        parts.append("<p>" + text(suitability["summary"]) + "</p>")
        for label, key in (("Что позволяют полученные данные", "supported_uses"), ("Что пока не обосновано", "unsubstantiated_uses"), ("Необходимые проверки", "required_checks")):
            parts.append("<h3>" + label + "</h3>" + lines(suitability[key]))
    else:
        parts.append("<p>Порядок основан на отдельных условных эффектах внутри одного выхода, единицы и исходной базы. Альтернативы одного замечания объединены по максимальному модулю изменения. Эффекты не складываются.</p>")
        for output in data["outputs"]:
            parts.append("<h3>" + text(output["label"]) + " · " + text(output['target']['sheet']+"!"+output['target']['cell']) + "</h3><p>Исходный расчёт: " + text(format_saved_value(output["baseline"])) + "; подпись единицы: " + text(output["raw_unit"]) + ".</p>")
            parts.append("<div class='table-scroll'><table><thead><tr><th>Приоритет</th><th>Замечание</th><th>Макс. модуль изменения</th><th>К модулю базы</th></tr></thead><tbody>")
            for item in output["ranked_findings"]:
                relative = item["max_absolute_relative_delta"]
                relative_text = ("Не определяется при нулевой базе" if relative is None else
                                 text(format_saved_value(relative*100))+"%" if math.isfinite(relative*100) else
                                 text(format_saved_value(relative))+" (доля)")
                parts.append("<tr><td>" + text(item["rank"]) + "</td><td>" + text(item["title"]) + "</td><td>" + text(format_saved_value(item["max_absolute_delta"])) + " " + text(output["raw_unit"]) + "</td><td>" + relative_text + "</td></tr>")
            parts.append("</tbody></table></div>")
            for item in output["ranked_findings"]:
                parts.append("<h4>" + text(item["title"]) + "</h4><p>" + text(item["reason"]) + "</p><p>" + text(item["action"]) + "</p>" + refs(item["evidence"]))
            parts.append("<p>" + text(output["conclusion"]) + "</p>")
        if not data["outputs"]:
            parts.append("<p>Измеренного влияния недостаточно для ранжирования. Это не означает отсутствия существенных проблем.</p>")
        if data["unquantified_findings"]:
            parts.append("<h3>Влияние не установлено</h3><p>Эти темы не имеют числового ранга и не считаются малозначимыми.</p>")
            for item in data["unquantified_findings"]:
                parts.append("<h4>" + text(item["title"]) + "</h4><p>" + text(item["reason"]) + "</p><p>" + text(item["action"]) + "</p>" + refs(item["evidence"]))
    parts.append("<p class='fine-print'>" + text(data["policy"]["meaning"]) + "</p></section>")
    return "".join(parts)
