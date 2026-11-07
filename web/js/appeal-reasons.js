// Коды API и короткие подписи в форме, очереди и карточке.
export const APPEAL_REASONS = {
  wrong_interpretation: 'Неверная трактовка',
  wrong_evidence: 'Не та реплика',
  wrong_na: 'Неверный N/A',
  other: 'Другое',
};
// Не причина оператора: так сохраняется ответ проверяющего на вопрос модели (голоса разошлись).
export const MODEL_DOUBT = 'model_doubt';
export const appealReasonLabel = (code) => (code === MODEL_DOUBT ? 'Модель сомневалась' : APPEAL_REASONS[code] || APPEAL_REASONS.other);
