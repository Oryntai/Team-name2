"""Синтетика для дообучения своей модели-судьи (LoRA): новые чаты → метки учителя → обучающие примеры.

  gen   — Claude Haiku пишет НОВЫЕ чаты в стиле DEV (бот AIM → эскалация → оператор) на RU/KZ/EN
          с нарушениями, заложенными по плану (план по каждому критерию хранится рядом с чатом);
  label — учитель Claude Sonnet оценивает их тем же судьёй продукта (cqa.judge: промпт и схема из чек-листа)
          по начальному чек-листу или его варианту (замена критерия, другие веса, без пояснений), чтобы ученик
          читал чек-лист, а не заучивал 8 критериев (на защите критерий заменят);
  build — обучающие примеры {prompt, completion}: промпт в шаблоне ученика (рендерит запущенный llama-server,
          тот же шаблон, что при работе) + ответ учителя в каноническом JSON.

DEV-чаты в обучение не идут: замер на 39 обучающих и 21 отложенном остаётся честным. В подсказки генератору
идут только 6 коротких чатов из ОБУЧАЮЩЕЙ части DEV — как образец формата.
Деньги: общий потолок --budget (по умолчанию $8), счётчик — по токенам из ответов API; новые запросы не
отправляются, когда потрачено ≥ потолка. Повторный запуск продолжает с места остановки.

  .venv/bin/python lab/local/synth.py gen --n 600 --conc 6            # Haiku; при 429 — перезапустить, допишет
  .venv/bin/python lab/local/synth.py label-batch --budget 7.7        # учитель Sonnet через Batch API (полцены)
  .venv/bin/python lab/local/synth.py label-batch --wait              # дождаться пачки и забрать метки
  .venv/bin/python lab/local/synth.py build --name qwen35 --max-miss 2
  (label — то же через обычный API, дороже вдвое и упирается в лимит запросов)
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import pathlib
import random
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cqa import checklist as ck  # noqa: E402
from cqa.judge import evaluate, get_provider  # noqa: E402
from cqa.judge.evaluate import _evidence, _parse  # noqa: E402
from cqa.judge.prompt import build_schema, build_system, build_user, normalize_answer  # noqa: E402
from cqa.judge.providers import PROVIDERS, Reply, cost_usd, env  # noqa: E402
from cqa.pii import mask_transcript  # noqa: E402

DATA = ROOT / "lab" / "local" / "data"
CHATS = DATA / "synth_chats.jsonl"
LABELS = DATA / "synth_labels.jsonl"
SPEND = DATA / "spend.json"
BATCH = DATA / "batch_state.json"
BATCH_DISCOUNT = 0.5  # Message Batches API — половина цены (справка claude-api)

_CLIENTS: dict[int, object] = {}


def _client():
    import anthropic
    key = id(asyncio.get_running_loop())
    if key not in _CLIENTS:  # base_url явно: в окружении Claude Code может стоять ANTHROPIC_BASE_URL
        _CLIENTS[key] = anthropic.AsyncAnthropic(api_key=env("ANTHROPIC_API_KEY"), base_url="https://api.anthropic.com",
                                                 timeout=300, max_retries=4)
    return _CLIENTS[key]


def _reply(r) -> Reply:
    u = r.usage
    return Reply(text=next((b.text for b in r.content if b.type == "text"), ""), model=r.model,
                 input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                 cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                 cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0, stop=r.stop_reason)


async def haiku_json(system: str, user: str, schema: dict, effort: str = "low", max_tokens: int = 8000) -> Reply:
    """Генерация чата: Haiku 5.5 со строгой схемой и низким effort (рассуждать тут почти не нужно)."""
    r = await _client().messages.create(
        model=PROVIDERS["haiku"]["model"], max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    return _reply(r)

LANG = {
    "RU": {"name": "русском", "greet": "Здравствуйте! Я AIM, AI-ассистент Freedom Broker. Я могу ответить на вопросы о продуктах и услугах, предоставить информацию по вашим счетам, активам и комиссиям, сравнить данные в таблице, отправить контакты вашего менеджера и передать вопрос специалистам поддержки.",
           "consent": "Продолжая разговор, Вы соглашаетесь на сбор и обработку персональных данных и подтверждаете, что ознакомились с Политикой конфиденциальности Компании.",
           "offer": "Желаете, чтобы я соединил Вас с оператором?", "wait": "Пожалуйста, ожидайте. Оператор скоро подключится к чату.",
           "esc": "Эскалация на оператора.", "verified": "Личность клиента подтверждена.", "lost": "Соединение с клиентом прервано.",
           "left": "Клиент покинул чат.", "closed": "Оператор закрыл чат.",
           "names": ["Лера", "Алина", "Дмитрий", "Айгерим", "Руслан", "Ольга", "Тимур", "Жанна", "Максим", "Асель"]},
    "KZ": {"name": "казахском", "greet": "Сәлеметсіз бе! Мен AIM, Freedom Broker компаниясының жасанды интеллект көмекшісімін. Өнімдер мен қызметтер туралы сұрақтарға жауап бере аламын, шоттарыңыз, активтеріңіз және комиссиялар туралы ақпарат бере аламын, деректерді кестеде салыстыра аламын, менеджеріңіздің байланыс деректерін жібере аламын және сұрағыңызды қолдау қызметіне жолдай аламын.",
           "consent": "Әңгімені жалғастыра отырып, дербес деректеріңіздің жиналуына және өңделуіне келісесіз әрі компанияның Құпиялылық саясатымен танысқаныңызды растайсыз.",
           "offer": "Сізді операторға қосайын ба?", "wait": "Күте тұрыңыз, оператор жақында чатқа қосылады.",
           "esc": "Чат операторға жіберілді.", "verified": "Клиенттің жеке басы расталды.", "lost": "Клиентпен байланыс үзілді.",
           "left": "Клиент чаттан шықты.", "closed": "Оператор чатты жапты.",
           "names": ["Мадина", "Айгерім", "Ерлан", "Динара", "Нұрлан", "Әсем", "Дәулет", "Жансая", "Арман", "Гүлнар"]},
    "EN": {"name": "английском", "greet": "Hello! I'm AIM, the Freedom Broker AI assistant. I can answer questions about products and services, provide information about your accounts, assets and fees, compare information in a table, share your account manager's contact details, and pass your question to our support team.",
           "consent": "By continuing this conversation, you agree to the collection and processing of your personal data and confirm that you have read the company's Privacy Policy.",
           "offer": "Would you like me to connect you with an agent?", "wait": "Please wait. An agent will join the chat shortly.",
           "esc": "Chat transferred to an agent.", "verified": "Customer identity verified.", "lost": "Connection to customer lost.",
           "left": "Customer left the chat.", "closed": "Agent closed the chat.",
           "names": ["Maya", "Alex", "Daniel", "Sara", "Tom", "Nina", "Chris", "Emma", "Ryan", "Lena"]},
}
STYLE_EXAMPLES = {"RU": ["p1-ru-003", "p1-ru-020"], "KZ": ["p1-kz-001", "p1-kz-007"], "EN": ["p1-en-001", "p1-en-010"]}

TOPICS = [
    "комиссия по тарифу при покупке акций на международном рынке", "где посмотреть свой тариф и как его сменить",
    "логин при регистрации занят (был старый закрытый счёт)", "пополнение счёта картой: деньги не пришли",
    "пополнение через банковский перевод: реквизиты и сроки", "вывод средств: заявка висит, нужна анкета",
    "как купить облигации и где посмотреть купоны", "маржинальная торговля: как включить и какие риски",
    "D-счёт: что это и как открыть", "форма 1042-S: где взять и зачем", "брокерский отчёт за год для налоговой",
    "смена паспортных данных (поручение «Изменение сведений»)", "участие в IPO: как подать заявку",
    "дивиденды не пришли на счёт", "форма W-8BEN: срок действия и продление", "двухфакторная аутентификация: не приходит код",
    "перевод бумаг от другого брокера", "конвертация валют в приложении и курс", "закрытие брокерского счёта",
    "смена номера телефона в профиле", "заблокирован вход после нескольких неверных паролей", "стоп-лосс и лимитная заявка",
    "торговля на KASE и AIX: в чём разница", "налоговое резидентство и удержание налога", "комиссия за депозитарное хранение",
    "приложение вылетает при открытии портфеля", "статус заявки на покупку: почему не исполнилась",
    "как скачать выписку по счёту", "открытие счёта для несовершеннолетнего", "бонус для новых клиентов и условия",
    "пополнение в долларах: какая комиссия банка", "сплит акций: почему изменилось количество бумаг",
    "консультация, во что вложить деньги", "как сменить тариф с «Инвестор» на другой", "отчёт о сделках за месяц на почту",
    "пароль от веб-кабинета не подходит", "перевод денег между своими счетами", "вопрос по налоговому вычету",
    "когда зачислятся купоны по облигациям", "проблема с подписанием поручения кодом из SMS",
]

# План по критериям: (ключ, вероятность, описание для генератора). {..} — подстановки по языку.
PLAN = {
    "01": [("ok", .74, "В первом ответе оператор здоровается на языке клиента и называет своё имя ({op})."),
           ("no_name", .1, "В первом ответе оператор здоровается, но НЕ называет своё имя (и в первом ответе не представляется)."),
           ("no_greeting", .08, "Первый ответ оператора сразу по делу, БЕЗ приветствия (имя можно не называть)."),
           ("wrong_lang", .08, "Оператор (включая приветствие и первый ответ) пишет по-русски, хотя клиент пишет на своём языке.")],
    "02": [("na", .45, "Клиент сразу (в первом сообщении или боту) сообщает всё, что нужно для ответа; оператор ничего не переспрашивает и отвечает по существу."),
           ("yes", .25, "Для ответа действительно не хватает одной детали (площадка, тип счёта, приложение или веб, валюта, дата и т.п.); оператор уточняет её ДО ответа, не переспрашивая известное, и затем отвечает."),
           ("no_reask", .18, "Клиент уже сообщил важную деталь (боту или в своём сообщении), а оператор всё равно переспрашивает её; клиент указывает, что уже писал."),
           ("no_guess", .12, "Для ответа не хватает важной детали, но оператор её не уточняет и отвечает наугад; ответ не подходит ситуации клиента.")],
    "03": [("na", .6, "Вопрос общий (тарифы, правила, где найти раздел): данные счёта клиента не раскрываются, защищённых действий нет, проверка личности не нужна."),
           ("yes", .25, "Клиент спрашивает о своём счёте или операции (баланс, статус вывода, сделка) или просит защищённое действие; оператор проводит проверку личности, появляется системное событие «{verified}», и только ПОСЛЕ него оператор раскрывает данные или выполняет действие."),
           ("no", .15, "Клиент спрашивает данные своего счёта или операции либо просит защищённое действие; оператор раскрывает данные или выполняет действие БЕЗ проверки личности (системного события о подтверждении личности нет), например «проверку можно пропустить».")],
    "04": [("20", .5, "Оператор полностью и верно решает запрос или даёт корректный следующий шаг (передал запрос и сказал, где и когда ждать ответа); клиенту понятно, что делать."),
           ("15", .15, "Основной вопрос решён верно, но пропущена второстепенная деталь, о которой клиент спрашивал."),
           ("10", .13, "Клиент задаёт два вопроса; оператор отвечает только на один, второй существенный вопрос остаётся без ответа."),
           ("5", .08, "Оператор даёт только общую подсказку («посмотрите в приложении»); клиенту так и непонятно, как действовать."),
           ("0", .07, "Оператор не отвечает по существу или даёт ошибочную ключевую рекомендацию."),
           ("na", .07, "Первое сообщение клиента расплывчатое («Здравствуйте, есть вопрос»); после подключения оператора клиент уходит, так и не сформулировав запрос (системное событие «{left}»).")],
    "05": [("ok", .75, "Оператор вежлив; если клиент выражает недовольство или неудобство — отзывается именно на эту проблему (признаёт, объясняет, решает)."),
           ("ignored", .15, "Клиент явно выражает недовольство или неудобство (долго ждал, уже писал, не работает); оператор вежлив, но никак не реагирует на это недовольство и отвечает шаблонно."),
           ("rude", .10, "Оператор отвечает пренебрежительно и раздражённо («читайте внимательнее», «это же очевидно»), но без прямых оскорблений.")],
    "06": [("10", .6, "Ответы оператора ясные, связные и по порядку; если оператор уходит что-то проверить, он предупреждает клиента об ожидании."),
           ("7", .2, "Один заметный сбой: одна реплика оператора невпопад или непонятна, либо оператор надолго уходит проверить без предупреждения; клиент всё же понимает и продолжает."),
           ("4", .13, "Из-за формулировок или порядка ответов оператора клиенту приходится переспрашивать (например, «не понял, что вы имеете в виду?»)."),
           ("0", .07, "Реплики оператора бессвязные или противоречат друг другу; ход диалога непонятен.")],
    "07": [("ok", .4, "В конце оператор подводит итог или предлагает дальнейшую помощь («Могу ещё чем-то помочь?») И прощается."),
           ("goodbye_only", .25, "В конце оператор только прощается («Хорошего дня!») — без итога и без предложения дальнейшей помощи."),
           ("no_goodbye", .2, "Оператор завершает без прощания: последняя реплика по делу, затем системное событие «{closed}»."),
           ("na", .15, "Чат обрывается до того, как оператор мог завершить: в конце системное событие «{lost}» или «{left}».")],
    "08": [("none", .8, "Грубых нарушений нет."),
           ("insult", .05, "Оператор оскорбляет клиента (например, называет его глупым или некомпетентным)."),
           ("private", .07, "Оператор предлагает клиенту платную консультацию в частном порядке: просит личный номер клиента или даёт свой личный номер, чтобы помочь за отдельную плату."),
           ("card", .05, "Оператор предлагает перевести деньги ему лично (на его карту), чтобы он сам их выгодно инвестировал."),
           ("disclose", .03, "Оператор раскрывает конфиденциальную информацию: данные другого клиента или внутренние данные компании.")],
}
EXPECT = {  # план → ответ, который ждём от учителя по начальному чек-листу
    "01": {"ok": "Да"}, "02": {"na": "N/A", "yes": "Да"}, "03": {"na": "N/A", "yes": "Да", "no": "Нет"},
    "05": {"ok": "Да"}, "07": {"ok": "Да", "na": "N/A"}, "08": {"none": "Нет"},
}


def expected(cid: str, key: str) -> str:
    if cid in ("04", "06"):
        return "N/A" if key == "na" else key
    if cid == "08":
        return EXPECT["08"].get(key, "Да")
    return EXPECT.get(cid, {}).get(key, "Нет")


def sample_plan(rng: random.Random, lang: str) -> dict:
    plan = {}
    for cid, opts in PLAN.items():
        keys = [k for k, _, _ in opts if not (cid == "01" and k == "wrong_lang" and lang == "RU")]
        weights = [p for k, p, _ in opts if k in keys]
        plan[cid] = rng.choices(keys, weights)[0]
    # не больше трёх нарушений в одном чате — иначе чат неправдоподобен
    viol = [c for c in plan if plan[c] not in ("ok", "na", "20", "10", "none")]
    rng.shuffle(viol)
    default = {"01": "ok", "02": "na", "03": "na", "04": "20", "05": "ok", "06": "10", "07": "ok", "08": "none"}
    for c in viol[3:]:
        plan[c] = default[c]
    if plan["04"] == "na":  # клиент ушёл, не сформулировав запрос
        plan.update({"02": "na", "03": "na", "05": "ok", "06": "10", "07": "na", "08": "none"})
    return plan


GEN_SYSTEM = """Ты пишешь реалистичные учебные транскрипты чатов поддержки брокерской компании Freedom Broker (Казахстан). Их используют, чтобы обучать систему контроля качества работы операторов, поэтому поведение оператора должно точно соответствовать заданному плану — и хорошее, и плохое.

Формат чата (как в примерах):
1. Клиент пишет первое сообщение.
2. Бот AIM отвечает ДОСЛОВНО двумя сообщениями (приветствие и согласие на обработку данных), затем пытается ответить по сути (коротко, часто не до конца).
3. Клиент просит оператора или уточняет; бот спрашивает, соединить ли с оператором (дословная фраза), клиент соглашается, бот просит подождать (дословная фраза), системное событие эскалации (дословно).
4. Клиент иногда дописывает детали, пока ждёт. Оператор подключается через 1–3 минуты; дальше разговор оператора и клиента.
5. Конец — как задано планом (прощание, закрытие чата оператором, уход клиента или обрыв связи — системными событиями дословно).

Требования:
- Метки времени — «MM:SS» от 00:00, не убывают; ответы оператора через 15–90 секунд, клиент — через 10–60 секунд.
- Роли: client, bot, operator, system. Системные события — только из списка ниже, дословно.
- Всего 10–22 реплики. Реплики короткие и живые, как в настоящих чатах; клиент может писать с опечатками.
- Язык чата задан: клиент, бот и (если план не говорит иначе) оператор пишут на нём. Названия разделов приложения можно оставлять по-русски («Кабинет», «Поручения»), как в примерах.
- Придумай НОВУЮ ситуацию на заданную тему; не копируй примеры, они только показывают формат и тон.
- Выполни КАЖДЫЙ пункт плана, но не подсказывай его в тексте (никаких пометок вроде «нарушение»)."""


def gen_user(plan: dict, lang: str, topic: str, op: str, examples: list[dict]) -> str:
    L = LANG[lang]
    sub = {"op": op, **{k: L[k] for k in ("verified", "lost", "left", "closed")}}
    lines = [f"Язык чата: на {L['name']} ({lang}). Тема запроса клиента: {topic}. Имя оператора: {op}.", "",
             "Дословные тексты для этого языка:",
             f"- бот, приветствие: «{L['greet']}»", f"- бот, согласие: «{L['consent']}»",
             f"- бот, предложение оператора: «{L['offer']}»", f"- бот, ожидание: «{L['wait']}»",
             f"- system, эскалация: «{L['esc']}»", f"- system, личность подтверждена: «{L['verified']}»",
             f"- system, обрыв связи: «{L['lost']}»", f"- system, клиент ушёл: «{L['left']}»",
             f"- system, оператор закрыл чат: «{L['closed']}»", "", "План поведения оператора по пунктам:"]
    names = {"01": "Приветствие и имя", "02": "Уточнение потребности", "03": "Проверка личности", "04": "Решение запроса",
             "05": "Вежливость и реакция на недовольство", "06": "Ясность и порядок ответов", "07": "Завершение",
             "08": "Грубые нарушения"}
    for cid in PLAN:
        desc = next(d for k, _, d in PLAN[cid] if k == plan[cid])
        lines.append(f"- {names[cid]}: {desc.format(**sub)}")
    lines += ["", "Примеры формата (другие темы):"]
    for ex in examples:
        lines.append(json.dumps({"transcript": ex["transcript"]}, ensure_ascii=False))
    return "\n".join(lines)


GEN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["transcript"], "properties": {
    "transcript": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                              "required": ["timestamp", "speaker", "text"], "properties": {
            "timestamp": {"type": "string"}, "speaker": {"type": "string", "enum": ["client", "bot", "operator", "system"]},
            "text": {"type": "string"}}}}}}


# ---------- варианты чек-листа (чтобы ученик читал чек-лист, а не заучивал его) ----------
NEW_CRITERIA = [
    {"title": "Обращение по имени", "type": "bool", "na": True, "yes": "оператор обращался к клиенту по имени.",
     "na_when": "имя клиента в чате неизвестно."},
    {"title": "Проверка понимания", "type": "bool", "na": False,
     "yes": "оператор убедился, что клиенту всё понятно или вопрос решён (спросил, получилось ли, остались ли вопросы)."},
    {"title": "Информирование о сроках", "type": "bool", "na": True,
     "yes": "оператор назвал сроки выполнения запроса или ответа.", "na_when": "запрос не предполагал сроков."},
    {"title": "Предложение дополнительной пользы", "type": "bool", "na": True,
     "yes": "оператор предложил полезную информацию или услугу по теме обращения сверх заданного вопроса.",
     "na_when": "повода для дополнительного предложения не было."},
    {"title": "Грамотность", "type": "scale", "na": False, "yes": "оцените грамотность реплик оператора по шкале.",
     "steps": [{"value": 10, "text": "Без ошибок и опечаток."}, {"value": 5, "text": "Одна-две ошибки или опечатки, смысл понятен."},
               {"value": 0, "text": "Много ошибок, они мешают пониманию."}]},
    {"title": "Деловой стиль", "type": "bool", "na": False,
     "yes": "оператор пишет в деловом стиле: без сленга, фамильярности, смайликов и капса."},
    {"title": "Скорость ответов", "type": "bool", "na": False,
     "yes": "оператор ответил впервые не позже чем через 3 минуты после эскалации и не оставлял клиента без ответа дольше 5 минут без предупреждения."},
    {"title": "Точный путь к решению", "type": "bool", "na": True,
     "yes": "оператор назвал точный путь в приложении или веб-кабинете (раздел, кнопку) либо дал ссылку на нужный раздел.",
     "na_when": "для ответа не нужно было никуда направлять клиента."},
    {"title": "Передача профильному отделу", "type": "bool", "na": True,
     "yes": "если вопрос вне компетенции оператора, он передал его профильному отделу и сообщил клиенту, где и когда ждать ответа.",
     "na_when": "оператор решил вопрос сам, передача не требовалась."},
    {"title": "Работа с возражениями", "type": "bool", "na": True,
     "yes": "если клиент не согласился или усомнился, оператор спокойно и аргументированно ответил на возражение.",
     "na_when": "клиент не возражал и не сомневался."},
    {"title": "Предупреждение о мошенничестве", "type": "bool", "na": True,
     "yes": "если в разговоре был повод (просьба сообщить код, подозрительный звонок или перевод), оператор предупредил клиента о мерах безопасности.",
     "na_when": "повода для предупреждения не было."},
    {"title": "Активное слушание", "type": "bool", "na": False,
     "yes": "оператор показал, что понял запрос: пересказал или подтвердил суть вопроса клиента своими словами."},
    {"title": "Полнота ответа", "type": "scale", "na": True, "yes": "оцените полноту ответа оператора по шкале.",
     "na_when": "клиент не задал вопроса по существу.",
     "steps": [{"value": 10, "text": "Ответ полный: закрыты все вопросы клиента."},
               {"value": 5, "text": "Ответ частичный: часть вопросов осталась без ответа."},
               {"value": 0, "text": "Ответа по существу нет."}]},
    {"title": "Персонализация ответа", "type": "bool", "na": False,
     "yes": "ответ учитывает конкретную ситуацию клиента, а не является общим шаблоном."},
]


def make_variant(base: dict, rng: random.Random) -> tuple[dict, str]:
    cfg = copy.deepcopy(base)
    kind = rng.choice(["replace", "replace", "replace", "weights", "noguide"])
    tags = []
    scored = [c for c in cfg["criteria"] if c["type"] != "flag"]
    if kind == "replace":
        old = rng.choice(scored)
        new = copy.deepcopy(rng.choice(NEW_CRITERIA))
        new_id = old["id"] if rng.random() < .6 else f"{len(cfg['criteria']) + 1:02d}"
        new.update({"id": new_id, "section": old.get("section", ""), "weight": old["weight"], "guidance": "",
                    "no": new.get("no", ""), "na_when": new.get("na_when", ""), "steps": new.get("steps", [])})
        idx = cfg["criteria"].index(old)
        cfg["criteria"][idx] = new
        if new_id != old["id"]:  # новый номер — критерий в конец секции не переносим, порядок как в интерфейсе
            pass
        tags.append(f"replace:{old['id']}->{new_id}:{new['title']}")
    if kind == "weights" or rng.random() < .3:
        for c in cfg["criteria"]:
            if c["type"] != "flag":
                c["weight"] = rng.choice([5, 10, 10, 15, 15, 20, 20, 25])
        tags.append("weights")
    if kind == "noguide" or rng.random() < .3:
        for c in cfg["criteria"]:
            c["guidance"] = ""
        tags.append("noguide")
    if rng.random() < .2:
        cfg["rules"] = []
        tags.append("norules")
    cfg["name"] = rng.choice(["Чек-лист качества чатов", "Чек-лист контроля качества", "Оценка оператора", "QA чек-лист v2"])
    errs = ck.validate(cfg)
    assert not errs, errs
    return ck.normalize(cfg), ",".join(tags)


# ---------- деньги ----------
def add_spend(usd: float, what: str) -> float:
    """Учёт трат в spend.json под файловой блокировкой (генерация и разметка могут идти параллельно)."""
    import fcntl
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / ".spend.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = json.loads(SPEND.read_text()) if SPEND.exists() else {"spent": 0.0, "by": {}}
        d["by"][what] = round(d["by"].get(what, 0.0) + usd, 6)
        d["spent"] = round(sum(d["by"].values()), 6)
        SPEND.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        return d["spent"]


class Budget:
    def __init__(self, cap: float):
        self.cap = cap
        self.spent = json.loads(SPEND.read_text())["spent"] if SPEND.exists() else 0.0
        self.lock = asyncio.Lock()

    def ok(self) -> bool:
        return self.spent < self.cap

    async def add(self, usd: float, what: str):
        async with self.lock:
            self.spent = add_spend(usd, what)


def read_jsonl(p: pathlib.Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def dev_records() -> dict:
    return {r["conversation_id"]: r for r in json.loads((ROOT / "data" / "dev.json").read_text())["data"]["records"]}


async def cmd_gen(args):
    DATA.mkdir(parents=True, exist_ok=True)
    budget = Budget(args.budget)
    split = json.loads((ROOT / "lab" / "holdout.json").read_text())
    recs = dev_records()
    ex = {lang: [recs[c] for c in ids] for lang, ids in STYLE_EXAMPLES.items()}
    assert all(c in split["train"] for ids in STYLE_EXAMPLES.values() for c in ids), "примеры — только из обучающей части"
    done = {r["conversation_id"] for r in read_jsonl(CHATS)}
    rng = random.Random(args.seed)
    jobs = []
    for i in range(1, args.n + 1):
        lang = rng.choices(["RU", "KZ", "EN"], [.4, .3, .3])[0]
        plan, topic = sample_plan(rng, lang), rng.choice(TOPICS)
        op = rng.choice(LANG[lang]["names"])
        example = rng.choice(ex[lang])  # тянуть всегда, иначе при повторном запуске собьётся последовательность
        cid = f"syn-{lang.lower()}-{i:04d}"
        if cid not in done:
            jobs.append((cid, lang, plan, topic, op, example))
    sem, lock, n_ok = asyncio.Semaphore(args.conc), asyncio.Lock(), [0]
    print(f"генерация: {len(jobs)} чатов (уже есть {len(done)}), потрачено ${budget.spent:.3f} из ${budget.cap}")

    async def one(job):
        cid, lang, plan, topic, op, example = job
        async with sem:
            if not budget.ok():
                return
            try:
                r = await haiku_json(GEN_SYSTEM, gen_user(plan, lang, topic, op, [example]), GEN_SCHEMA)
            except Exception as e:  # noqa: BLE001
                print(f"  {cid}: {str(e)[:150]}")
                return
            await budget.add(cost_usd("haiku", r), "gen-haiku")
            try:
                tr = json.loads(r.text)["transcript"]
            except Exception:  # noqa: BLE001
                return
            if not (8 <= len(tr) <= 30) or not any(t["speaker"] == "operator" for t in tr):
                return
            rec = {"conversation_id": cid, "channel": "agent chat", "language": lang, "transcript": tr,
                   "plan": plan, "topic": topic, "operator_name": op, "style_example": example["conversation_id"]}
            async with lock:
                with CHATS.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_ok[0] += 1

    t0 = time.monotonic()
    await asyncio.gather(*[one(j) for j in jobs])
    print(f"готово: +{n_ok[0]} чатов за {time.monotonic() - t0:.0f} с; всего потрачено ${budget.spent:.3f}")


async def cmd_label(args):
    budget = Budget(args.budget)
    base = ck.load_default()
    chats = read_jsonl(CHATS)[: args.limit or None]
    done = {r["conversation_id"] for r in read_jsonl(LABELS)}
    rng = random.Random(args.seed)
    jobs = []
    for rec in chats:
        cfg, tag = (make_variant(base, rng) if rng.random() < args.variant_share else (base, "default"))
        if rec["conversation_id"] not in done:
            jobs.append((rec, cfg, tag))
    prov = get_provider(args.teacher)
    sem, lock, n_ok = asyncio.Semaphore(args.conc), asyncio.Lock(), [0]
    print(f"разметка учителем {args.teacher}: {len(jobs)} чатов (уже есть {len(done)}), потрачено ${budget.spent:.3f} из ${budget.cap}")

    async def one(rec, cfg, tag):
        async with sem:
            if not budget.ok():
                return
            res = await evaluate({k: rec[k] for k in ("conversation_id", "channel", "language", "transcript")}, cfg, prov)
            await budget.add(res.get("cost_usd") or 0.0, f"label-{args.teacher}")
            if res["status"] != "ok":
                print(f"  {rec['conversation_id']}: {res['error'][:150]}")
                return
            out = {"conversation_id": rec["conversation_id"], "teacher": res["model"], "variant": tag, "checklist": cfg,
                   "answers": res["answers"], "cost_usd": res["cost_usd"], "pii_masked": res["pii_masked"]}
            async with lock:
                with LABELS.open("a") as f:
                    f.write(json.dumps(out, ensure_ascii=False) + "\n")
                n_ok[0] += 1

    t0 = time.monotonic()
    await asyncio.gather(*[one(*j) for j in jobs])
    print(f"готово: +{n_ok[0]} меток за {time.monotonic() - t0:.0f} с; всего потрачено ${budget.spent:.3f}")


def batch_requests(chats: list[dict], base: dict, args) -> tuple[list[dict], dict]:
    """Запросы для Message Batches: ровно тот же промпт и схема, что у судьи продукта (cqa.judge)."""
    rng = random.Random(args.seed)
    reqs, meta = [], {}
    for rec in chats:
        cfg, tag = (make_variant(base, rng) if rng.random() < args.variant_share else (base, "default"))
        masked, pii = mask_transcript(rec["transcript"])
        params = {"model": PROVIDERS[args.teacher]["model"], "max_tokens": 16000,
                  "system": [{"type": "text", "text": build_system(cfg), "cache_control": {"type": "ephemeral"}}],
                  "messages": [{"role": "user", "content": build_user(rec, masked)}],
                  "output_config": {"effort": PROVIDERS[args.teacher].get("effort", "medium"),
                                    "format": {"type": "json_schema", "schema": build_schema(cfg)}}}
        reqs.append({"custom_id": rec["conversation_id"], "params": params})
        meta[rec["conversation_id"]] = {"variant": tag, "checklist": cfg, "pii_masked": pii}
    return reqs, meta


def parse_answers(text: str, cfg: dict, n_replies: int) -> tuple[dict | None, str | None]:
    """Разбор ответа учителя тем же способом, что в cqa.judge.evaluate (без «починки» правилами)."""
    js = _parse(text)
    crit = (js or {}).get("criteria") if isinstance(js, dict) else None
    if not isinstance(crit, dict):
        return None, "не JSON чек-листа"
    answers = {}
    for c in cfg["criteria"]:
        item = crit.get(c["id"])
        a = normalize_answer(c, item.get("answer")) if isinstance(item, dict) else None
        if a is None:
            return None, f"{c['id']}: нет допустимого ответа"
        answers[c["id"]] = {"answer": a, "explanation": str(item.get("explanation") or "").strip(),
                            "evidence": _evidence(item.get("evidence"), n_replies)}
    return answers, None


def cmd_label_batch(args):
    """Метки учителя через Message Batches API (вдвое дешевле, ответ обычно в пределах часа).
    Первый запуск отправляет пачку и запоминает её id; повторный — ждёт окончания и забирает результаты."""
    import anthropic
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), base_url="https://api.anthropic.com", max_retries=4)
    base = ck.load_default()
    chats = {r["conversation_id"]: r for r in read_jsonl(CHATS)
             if r["conversation_id"].startswith(args.only)}
    if args.limit:
        chats = dict(list(chats.items())[: args.limit])
    if not BATCH.exists():
        done = {r["conversation_id"] for r in read_jsonl(LABELS)}
        todo = [r for c, r in chats.items() if c not in done]
        spent = json.loads(SPEND.read_text())["spent"] if SPEND.exists() else 0.0
        est = len(todo) * args.est_per_chat
        if spent + est > args.budget:
            keep = max(0, int((args.budget - spent) / args.est_per_chat))
            print(f"прогноз ${est:.2f} + потрачено ${spent:.2f} > потолка ${args.budget}: отправляю {keep} из {len(todo)}")
            todo = todo[:keep]
        if not todo:
            print("нечего размечать")
            return
        reqs, meta = batch_requests(todo, base, args)
        b = client.messages.batches.create(requests=reqs)
        BATCH.write_text(json.dumps({"id": b.id, "teacher": args.teacher, "created": time.strftime("%H:%M:%S"),
                                     "meta": meta}, ensure_ascii=False))
        print(f"пачка {b.id}: {len(reqs)} запросов, прогноз ≈ ${len(reqs) * args.est_per_chat:.2f}")
    st = json.loads(BATCH.read_text())
    while True:
        b = client.messages.batches.retrieve(st["id"])
        rc = b.request_counts
        print(f"{time.strftime('%H:%M:%S')} пачка {b.processing_status}: готово {rc.succeeded}, ошибок {rc.errored}, "
              f"в работе {rc.processing}", flush=True)
        if b.processing_status == "ended" or not args.wait:
            break
        time.sleep(30)
    if b.processing_status != "ended":
        return
    budget_spent, n_ok, errs = 0.0, 0, []
    with LABELS.open("a") as f:
        for res in client.messages.batches.results(st["id"]):
            cid, m = res.custom_id, st["meta"][res.custom_id]
            if res.result.type != "succeeded":
                errs.append(f"{cid}: {res.result.type}")
                continue
            msg = res.result.message
            rep = _reply(msg)
            cost = cost_usd(st["teacher"], rep) * BATCH_DISCOUNT
            budget_spent += cost
            if msg.stop_reason != "end_turn":
                errs.append(f"{cid}: stop {msg.stop_reason}")
                continue
            answers, err = parse_answers(rep.text, m["checklist"], len(chats[cid]["transcript"]))
            if err:
                errs.append(f"{cid}: {err}")
                continue
            f.write(json.dumps({"conversation_id": cid, "teacher": msg.model, "variant": m["variant"],
                                "checklist": m["checklist"], "answers": answers, "cost_usd": round(cost, 6),
                                "pii_masked": m["pii_masked"], "via": "batch"}, ensure_ascii=False) + "\n")
            n_ok += 1
    total = add_spend(budget_spent, f"label-{st['teacher']}-batch")
    BATCH.rename(DATA / f"batch_{st['id']}.json")
    print(f"метки: +{n_ok}, ошибок {len(errs)}; пачка стоила ${budget_spent:.3f}; всего потрачено ${total:.3f}")
    for e in errs[:10]:
        print("  " + e)


SCALE = {"04": ["20", "15", "10", "5", "0"], "06": ["10", "7", "4", "0"]}


def plan_agreement(rec: dict, lab: dict) -> tuple[int, int, list[str]]:
    """Сколько пунктов плана учитель подтвердил (по начальным критериям, которые не заменены в варианте).
    Для шкал допускаем соседнюю ступень: план задаёт поведение, а ступень — решение учителя."""
    ids = {c["id"]: c for c in lab["checklist"]["criteria"]}
    base = {c["id"]: c for c in ck.load_default()["criteria"]}
    agree, total, miss = 0, 0, []
    for cid, key in rec["plan"].items():
        if cid not in ids or ids[cid]["title"] != base[cid]["title"]:
            continue
        exp, got = expected(cid, key), lab["answers"][cid]["answer"]
        total += 1
        near = (cid in SCALE and exp in SCALE[cid] and got in SCALE[cid]
                and abs(SCALE[cid].index(exp) - SCALE[cid].index(got)) <= 1)
        if exp == got or near:
            agree += 1
        else:
            miss.append(f"{cid}:{key}->{got}")
    return agree, total, miss


def canonical_json(cfg: dict, answers: dict) -> str:
    out = {c["id"]: {"explanation": answers[c["id"]]["explanation"], "answer": answers[c["id"]]["answer"],
                     "evidence": answers[c["id"]]["evidence"]} for c in cfg["criteria"]}
    return json.dumps({"criteria": out}, ensure_ascii=False, separators=(",", ":"))


# Шаблон Qwen3.5 при --reasoning off; сверен байт в байт с /apply-template llama-server b11669 (5 примеров).
QWEN35_TEMPLATE = ("<|im_start|>system\n{s}<|im_end|>\n<|im_start|>user\n{u}<|im_end|>\n"
                   "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def render(server: str, system: str, user: str) -> str:
    if server == "qwen35":
        return QWEN35_TEMPLATE.format(s=system, u=user)
    body = json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/apply-template", body, {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())["prompt"]


def cmd_build(args):
    chats = {r["conversation_id"]: r for r in read_jsonl(CHATS)}
    labels = read_jsonl(LABELS)
    rows, dropped = [], []
    for lab in labels:
        rec = chats[lab["conversation_id"]]
        agree, total, miss = plan_agreement(rec, lab)
        if total and total - agree > args.max_miss:
            dropped.append((rec["conversation_id"], miss))
            continue
        tr = rec["transcript"]
        masked, _ = mask_transcript(tr)
        system, user = build_system(lab["checklist"]), build_user(rec, masked)
        prompt = render(args.server, system, user)
        rows.append({"id": rec["conversation_id"], "lang": rec["language"], "variant": lab["variant"],
                     "prompt": prompt, "completion": canonical_json(lab["checklist"], lab["answers"]) + args.eos})
    out = DATA / f"train_{args.name}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    by_var = sum(r["variant"] != "default" for r in rows)
    print(f"обучающих примеров: {len(rows)} (из них на вариантах чек-листа {by_var}); отброшено по несогласию с планом: "
          f"{len(dropped)} → {out.relative_to(ROOT)}")
    for cid, miss in dropped[:15]:
        print(f"  {cid}: {', '.join(miss)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--n", type=int, default=600)
    g.add_argument("--conc", type=int, default=24)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--budget", type=float, default=8.0)
    lb = sub.add_parser("label")
    lb.add_argument("--teacher", default="sonnet")
    lb.add_argument("--conc", type=int, default=16)
    lb.add_argument("--limit", type=int, default=0)
    lb.add_argument("--variant-share", type=float, default=0.3)
    lb.add_argument("--seed", type=int, default=11)
    lb.add_argument("--budget", type=float, default=8.0)
    bb = sub.add_parser("label-batch")
    bb.add_argument("--teacher", default="sonnet")
    bb.add_argument("--limit", type=int, default=0)
    bb.add_argument("--variant-share", type=float, default=0.3)
    bb.add_argument("--seed", type=int, default=11)
    bb.add_argument("--budget", type=float, default=8.0)
    bb.add_argument("--est-per-chat", type=float, default=0.0065, help="прогноз цены метки в пачке, $")
    bb.add_argument("--wait", action="store_true", help="ждать окончания пачки и забрать результаты")
    bb.add_argument("--only", default="", help="только чаты с таким префиксом id (например syn-g-)")
    b = sub.add_parser("build")
    b.add_argument("--server", default="qwen35", help="qwen35 — шаблон строкой; иначе адрес llama-server")
    b.add_argument("--name", default="qwen35")
    b.add_argument("--eos", default="<|im_end|>")
    b.add_argument("--max-miss", type=int, default=1)
    args = ap.parse_args()
    if args.cmd == "gen":
        asyncio.run(cmd_gen(args))
    elif args.cmd == "label":
        asyncio.run(cmd_label(args))
    elif args.cmd == "label-batch":
        cmd_label_batch(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
