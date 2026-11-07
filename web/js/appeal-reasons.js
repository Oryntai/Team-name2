// Коды API и короткие подписи в форме, очереди и карточке.
export const APPEAL_REASONS = {
  wrong_interpretation: 'Неверная трактовка',
  wrong_evidence: 'Не та реплика',
  wrong_na: 'Неверный N/A',
  other: 'Другое',
};
export const appealReasonLabel = (code) => APPEAL_REASONS[code] || APPEAL_REASONS.other;
