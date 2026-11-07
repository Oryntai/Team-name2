// Общие короткие подписи разговорной аналитики.
export const INSIGHT_FLAGS = {
  complaint: 'Недовольство', escalation_request: 'Жалоба', churn_risk: 'Риск ухода',
  follow_up_promised: 'Обещан ответ', unresolved: 'Не решено', bot_failed: 'Сбой бота',
};
export const SENTIMENT = { negative: 'Негативное', neutral: 'Нейтральное', positive: 'Позитивное' };
export const insightBusy = (row) => ['pending', 'running'].includes(row?.data?.status);
