"""Добавка к синтетике: грубые нарушения (08), которые генератор Haiku писать отказывается.

Haiku не пишет оператору оскорбления и просьбы перевести деньги на личную карту даже по плану (проверено: из ~70 таких
пунктов плана учитель нашёл единицы). Поэтому берём НЕразмеченные синтетические чаты и программно вставляем одну
реплику оператора с нарушением перед его последней репликой. Метки ставит тот же учитель (label-batch), как обычно.

  .venv/bin/python lab/local/inject_gross.py --n 45     # → дописывает чаты syn-g-*-NNNN в synth_chats.jsonl
"""
import argparse
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lab" / "local"))
import synth  # noqa: E402

LINES = {
    "insult": {
        "RU": ["Честно, вопрос глупый. Вы вообще понимаете, куда вкладываете деньги?",
               "С вашим уровнем понимания лучше вообще не лезть в инвестиции, не тратьте моё время."],
        "KZ": ["Шынымды айтсам, сұрағыңыз ақымақ. Ақшаңызды қайда салып жатқаныңызды түсінесіз бе өзі?",
               "Мұндай түсінікпен инвестицияға араласпағаныңыз дұрыс, уақытымды алмаңыз."],
        "EN": ["Honestly, that's a stupid question. Do you even understand where you're putting your money?",
               "With your level of understanding you shouldn't be investing at all. Don't waste my time."],
    },
    "card": {
        "RU": ["Кстати, если хотите доходность выше, переведите деньги мне на карту 4400 4302 1234 5678, я сам их вложу выгоднее, чем в приложении.",
               "Могу сам вложить ваши деньги выгоднее: переведите сумму мне на личную карту, реквизиты скину сюда."],
        "KZ": ["Айтпақшы, табысыңыз жоғары болсын десеңіз, ақшаны маған 4400 4302 1234 5678 картасына аударыңыз, қосымшадан гөрі тиімдірек салып беремін.",
               "Ақшаңызды өзім тиімдірек инвестициялай аламын: соманы менің жеке картама аударыңыз, деректемелерді осында жіберемін."],
        "EN": ["By the way, if you want better returns, transfer the money to my card 4400 4302 1234 5678 and I'll invest it for you better than the app would.",
               "I can invest your money personally for a better return: just send the amount to my personal card, I'll share the details here."],
    },
    "private": {
        "RU": ["Могу подробно проконсультировать в частном порядке за отдельную плату — напишите мне в WhatsApp на личный номер +7 707 123 45 67.",
               "Оставьте свой личный номер, я перезвоню после работы и за небольшую плату разберу ваш портфель лично."],
        "KZ": ["Жеке тәртіппен, бөлек ақыға толық кеңес бере аламын — WhatsApp арқылы жеке нөміріме жазыңыз: +7 707 123 45 67.",
               "Жеке нөміріңізді қалдырыңыз, жұмыстан кейін хабарласып, аздаған ақыға портфеліңізді жеке талдап беремін."],
        "EN": ["I can give you a detailed private consultation for a separate fee — message me on WhatsApp at my personal number +7 707 123 45 67.",
               "Leave me your personal number, I'll call you after work and review your portfolio privately for a small fee."],
    },
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=45)
    ap.add_argument("--seed", type=int, default=21)
    args = ap.parse_args()
    chats = synth.read_jsonl(synth.CHATS)
    labeled = {r["conversation_id"] for r in synth.read_jsonl(synth.LABELS)}
    free = [r for r in chats if r["conversation_id"] not in labeled and not r["conversation_id"].startswith("syn-g-")
            and r["plan"]["08"] == "none" and r["plan"]["04"] != "na"]
    rng = random.Random(args.seed)
    rng.shuffle(free)
    kinds = ["insult", "card", "private"]
    out = []
    for i, rec in enumerate(free[: args.n]):
        kind = kinds[i % 3]
        tr = [dict(t) for t in rec["transcript"]]
        ops = [j for j, t in enumerate(tr) if t["speaker"] == "operator"]
        if len(ops) < 2:
            continue
        at = ops[-1]  # перед последней репликой оператора, с той же меткой времени
        tr.insert(at, {"timestamp": tr[at]["timestamp"], "speaker": "operator",
                       "text": rng.choice(LINES[kind][rec["language"]])})
        new = {**rec, "conversation_id": f"syn-g-{rec['language'].lower()}-{i + 1:04d}", "transcript": tr,
               "plan": {**rec["plan"], "08": kind}, "injected": {"kind": kind, "from": rec["conversation_id"], "at": at + 1}}
        out.append(new)
    with synth.CHATS.open("a") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"добавлено {len(out)} чатов с грубыми нарушениями: " +
          ", ".join(f"{k} {sum(r['injected']['kind'] == k for r in out)}" for k in kinds))


if __name__ == "__main__":
    main()
