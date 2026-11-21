export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('Сервис вернул нечитаемый ответ.'); }
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос.';
    throw new Error(detail);
  }
  return data;
}
export const STAGES = [
  ['audit', 'Аудит расчётов'], ['assumptions', 'Предпосылки'],
  ['quality', 'Качество модели'], ['business', 'Обзор бизнеса'],
];
export const STATUS = {queued: 'В очереди', running: 'Анализируем', completed: 'Отчёт сформирован', failed: 'Ошибка', cancelled: 'Остановлено', limited: 'С ограничениями', not_verified: 'Не проверено', skipped: 'Не выполнялся', pending: 'Ожидает'};
export function displayValue(value) {
  if (value === null || value === undefined) return 'Нет сохранённого значения';
  if (typeof value === 'number') return new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 6}).format(value);
  return String(value);
}
