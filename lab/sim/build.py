"""Симуляция контрольного набора: 50 новых чатов (20 RU, 15 KZ, 15 EN) с эталоном по чек-листу из PDF.

Ситуации придуманы заново (не из DEV), эталон выставлен по тексту чек-листа (стр. 1–3) глазами
специалиста отдела качества. Сведения о продуктах в репликах условные: считаются верными, если в
сценарии не сказано обратное (ошибка оператора всегда проверяется по самому диалогу или общеизвестному
факту, например часы работы NYSE).

Эталон: gold — ответы 01..08; alt — тоже допустимые ответы в спорных местах (с причиной в notes);
disputed — критерии, где чек-лист не даёт однозначного ответа (в строгой метрике не учитываются).
Запуск: .venv/bin/python lab/sim/build.py  → lab/sim/sim_chats.json (как ответ API) и lab/sim/sim_gold.json
"""
from __future__ import annotations

import json
import pathlib

from scenarios import CHATS

HERE = pathlib.Path(__file__).resolve().parent
CODES = ["01", "02", "03", "04", "05", "06", "07", "08"]
WEIGHTS = {"01": 10, "02": 15, "03": 20, "04": 20, "05": 15, "06": 15, "07": 5}
SCALE_MAX = {"04": 20, "06": 10}

TEXT = {
    "RU": {
        "intro": ["Здравствуйте! Я AIM, AI-ассистент Freedom Broker. Я могу ответить на вопросы о продуктах и услугах, предоставить информацию по вашим счетам, активам и комиссиям, сравнить данные в таблице, отправить контакты вашего менеджера и передать вопрос специалистам поддержки.",
                  "Продолжая разговор, Вы соглашаетесь на сбор и обработку персональных данных и подтверждаете, что ознакомились с Политикой конфиденциальности Компании."],
        "esc": ["Желаете, чтобы я соединил Вас с оператором?", "да", "Пожалуйста, ожидайте. Оператор скоро подключится к чату.", "Эскалация на оператора."],
        "verified": "Личность клиента подтверждена.", "left": "Клиент покинул чат.",
        "closed": "Оператор закрыл чат.", "lost": "Соединение с клиентом прервано.",
    },
    "KZ": {
        "intro": ["Сәлеметсіз бе! Мен AIM, Freedom Broker компаниясының жасанды интеллект көмекшісімін. Өнімдер мен қызметтер туралы сұрақтарға жауап бере аламын, шоттарыңыз, активтеріңіз және комиссиялар туралы ақпарат бере аламын, деректерді кестеде салыстыра аламын, менеджеріңіздің байланыс деректерін жібере аламын және сұрағыңызды қолдау қызметіне жолдай аламын.",
                  "Әңгімені жалғастыра отырып, дербес деректеріңіздің жиналуына және өңделуіне келісесіз әрі компанияның Құпиялылық саясатымен танысқаныңызды растайсыз."],
        "esc": ["Сізді операторға қосайын ба?", "Иә", "Күте тұрыңыз, оператор жақында чатқа қосылады.", "Чат операторға жіберілді."],
        "verified": "Клиенттің жеке басы расталды.", "left": "Клиент чаттан шықты.",
        "closed": "Оператор чатты жапты.", "lost": "Клиентпен байланыс үзілді.",
    },
    "EN": {
        "intro": ["Hello! I'm AIM, the Freedom Broker AI assistant. I can answer questions about products and services, provide information about your accounts, assets and fees, compare information in a table, share your account manager's contact details, and pass your question to our support team.",
                  "By continuing this conversation, you agree to the collection and processing of your personal data and confirm that you have read the company's Privacy Policy."],
        "esc": ["Would you like me to connect you with an agent?", "yes", "Please wait, an agent will join the chat shortly.", "Chat transferred to an agent."],
        "verified": "Customer identity verified.", "left": "Customer left the chat.",
        "closed": "Agent closed the chat.", "lost": "Connection to customer lost.",
    },
}
ROLE = {"c": "client", "o": "operator", "b": "bot", "s": "system"}


def sec(ts: str) -> int:
    m, s = ts.split(":")
    return int(m) * 60 + int(s)


def fmt(n: int) -> str:
    return f"{n // 60:02d}:{n % 60:02d}"


def transcript(lang: str, script: str) -> list[dict]:
    t = TEXT[lang]
    out: list[dict] = []
    for raw in script.strip().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        ts, rest = raw.split(" ", 1)
        n = sec(ts)
        if rest == "@intro":
            out += [{"timestamp": fmt(n), "speaker": "bot", "text": t["intro"][0]},
                    {"timestamp": fmt(n + 1), "speaker": "bot", "text": t["intro"][1]}]
        elif rest == "@esc":
            b1, c1, b2, s1 = t["esc"]
            out += [{"timestamp": fmt(n), "speaker": "bot", "text": b1},
                    {"timestamp": fmt(n + 4), "speaker": "client", "text": c1},
                    {"timestamp": fmt(n + 5), "speaker": "bot", "text": b2},
                    {"timestamp": fmt(n + 6), "speaker": "system", "text": s1}]
        elif rest.startswith("s @"):
            out.append({"timestamp": fmt(n), "speaker": "system", "text": t[rest[3:]]})
        else:
            role, text = rest.split(" ", 1)
            out.append({"timestamp": fmt(n), "speaker": ROLE[role], "text": text})
    times = [sec(x["timestamp"]) for x in out]
    assert times == sorted(times), "время идёт назад"
    return out


def parse_gold(s: str) -> dict:
    vals = s.split()
    assert len(vals) == 8, s
    out = {}
    for code, v in zip(CODES, vals):
        out[code] = int(v) if v.isdigit() else v
    return out


def final_score(g: dict) -> float | None:
    got = total = 0.0
    for code, w in WEIGHTS.items():
        v = g[code]
        if v == "N/A":
            continue
        total += w
        if code in SCALE_MAX:
            got += w * v / SCALE_MAX[code]
        elif v == "Да":
            got += w
    return None if total == 0 else round(got / total * 100 + 1e-9, 2)


def main() -> None:
    records, gold = [], []
    seen = set()
    for ch in CHATS:
        cid = ch["id"]
        assert cid not in seen, cid
        seen.add(cid)
        lang = cid.split("-")[1].upper()
        g = parse_gold(ch["gold"])
        assert g["04"] in ("N/A", 0, 5, 10, 15, 20) and g["06"] in (0, 4, 7, 10), cid
        for code in ("01", "05", "08"):
            assert g[code] in ("Да", "Нет"), (cid, code)
        for code in ("02", "03", "07"):
            assert g[code] in ("Да", "Нет", "N/A"), (cid, code)
        tr = transcript(lang, ch["chat"])
        assert any(x["speaker"] == "operator" for x in tr), cid
        records.append({"conversation_id": cid, "channel": "agent chat", "language": lang,
                        "transcript": tr, "audio_url": None})
        alt = {k: [int(x) if str(x).isdigit() else x for x in v] for k, v in ch.get("alt", {}).items()}
        gold.append({"conversation_id": cid, "language": lang, "scenario": ch["sc"], "gold": g,
                     "final_score": final_score(g), "alt": alt, "notes": ch.get("notes", {}),
                     "disputed": sorted(alt)})
    api = {"error": "", "isSuccess": True,
           "data": {"dataset_group_id": "SIM", "total_records": len(records), "records": records}}
    (HERE / "sim_chats.json").write_text(json.dumps(api, ensure_ascii=False, indent=1))
    (HERE / "sim_gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1))
    langs = {}
    for r in records:
        langs[r["language"]] = langs.get(r["language"], 0) + 1
    print(f"{len(records)} чатов {langs}; спорных решений {sum(len(x['alt']) for x in gold)} из {8 * len(gold)}")


if __name__ == "__main__":
    main()
