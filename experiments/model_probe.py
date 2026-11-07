"""Small, local probe of three pretrained models on the organizer DEV set.

Usage:
  python experiments/model_probe.py speaker --dataset data/dev.json --audio data/sample.mp3
  python experiments/model_probe.py emotion --dataset data/dev.json --audio data/sample.mp3
  python experiments/model_probe.py checklist --dataset data/dev.json

Audio must correspond to voice conversation p4-ru-003. No signed API URL is stored here.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path


VOICE_ID = "p4-ru-003"
CHAT_ID = "p1-ru-001"


def record(dataset, conversation_id):
    payload = json.loads(Path(dataset).read_text(encoding="utf-8"))
    return next(x for x in payload["data"]["records"] if x["conversation_id"] == conversation_id)


def voice_segments(row, audio_path, count=8):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    audio, sample_rate = sf.read(audio_path)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        from math import gcd
        factor = gcd(sample_rate, 16000)
        audio = resample_poly(audio, 16000 // factor, sample_rate // factor)
    base = datetime.fromisoformat(row["transcript"][0]["timestamp"])
    duration = len(audio) / 16000
    candidates = []
    turns = row["transcript"]
    for i, turn in enumerate(turns[:-1]):
        if turn["speaker"] not in ("operator", "client") or len(turn["text"]) < 18:
            continue
        start = (datetime.fromisoformat(turn["timestamp"]) - base).total_seconds() + 0.25
        next_start = (datetime.fromisoformat(turns[i + 1]["timestamp"]) - base).total_seconds()
        end = min(start + 3.0, next_start - 0.1, duration)
        if end - start < 1.8:
            continue
        clip = np.asarray(audio[int(start * 16000):int(end * 16000)], dtype=np.float32)
        if np.sqrt(np.mean(clip ** 2)) < 0.004:
            continue
        candidates.append({"index": i, "speaker": turn["speaker"], "start": round(start, 2), "end": round(end, 2), "audio": clip})
    # Spread samples across the call while retaining both known speaker roles.
    result = []
    for speaker in ("operator", "client"):
        subset = [x for x in candidates if x["speaker"] == speaker]
        if subset:
            picks = np.linspace(0, len(subset) - 1, min(count // 2, len(subset)), dtype=int)
            result.extend(subset[j] for j in picks)
    return sorted(result, key=lambda x: x["start"])


def speaker_probe(args):
    import numpy as np
    import torch
    from sklearn.cluster import KMeans
    from transformers import Wav2Vec2FeatureExtractor, WavLMForXVector

    segments = voice_segments(record(args.dataset, VOICE_ID), args.audio)
    model_id = "microsoft/wavlm-base-plus-sv"
    extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_id)
    model = WavLMForXVector.from_pretrained(model_id).eval()
    vectors = []
    with torch.inference_mode():
        for segment in segments:
            inputs = extractor(segment["audio"], sampling_rate=16000, return_tensors="pt")
            vector = model(**inputs).embeddings[0]
            vectors.append(torch.nn.functional.normalize(vector, dim=0).numpy())
    clusters = KMeans(n_clusters=2, random_state=0, n_init=10).fit_predict(vectors)
    actual = np.array([int(s["speaker"] == "operator") for s in segments])
    agreement = max(np.mean(clusters == actual), np.mean(1 - clusters == actual))
    return {"model": model_id, "conversation_id": VOICE_ID, "method": "transcript-timed clips + speaker embeddings + 2-means", "segments": [{k: v for k, v in s.items() if k != "audio"} | {"cluster": int(c)} for s, c in zip(segments, clusters)], "role_agreement": round(float(agreement), 3)}


def emotion_probe(args):
    import torch
    from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2ForSequenceClassification

    segments = voice_segments(record(args.dataset, VOICE_ID), args.audio, count=6)
    model_id = "superb/wav2vec2-base-superb-er"
    extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_id)
    model = Wav2Vec2ForSequenceClassification.from_pretrained(model_id).eval()
    results = []
    with torch.inference_mode():
        for segment in segments:
            inputs = extractor(segment["audio"], sampling_rate=16000, return_tensors="pt")
            probabilities = model(**inputs).logits.softmax(dim=-1)[0]
            index = int(probabilities.argmax())
            results.append({k: v for k, v in segment.items() if k != "audio"} | {"label": model.config.id2label[index], "confidence": round(float(probabilities[index]), 3)})
    return {"model": model_id, "conversation_id": VOICE_ID, "segments": results, "note": "Four generic emotion classes; no sarcasm label or ground-truth emotions."}


def checklist_probe(args):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    row = record(args.dataset, CHAT_ID)
    turns = "\n".join(f"[{i}] {x['speaker']}: {x['text']}" for i, x in enumerate(row["transcript"]))
    model_id = "Qwen/Qwen2.5-0.5B-Instruct"
    model_path = os.environ.get("CQA_QWEN_MODEL_PATH", model_id)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(model_path, dtype=torch.float32).eval()
    system = "Ты аудитор качества чата брокера. Оценивай только реплики operator; bot и system — контекст. Не придумывай фактов. Ответь одним JSON-объектом без Markdown."
    user = f"""Оцени диалог по критериям и верни JSON с ключами c1..c8. Значение каждого: {{"answer": "Да|Нет|N/A|0|5|10|15|20|4|7", "evidence": номер реплики или null, "reason": коротко}}.
c1 Контакт, Да если оператор поздоровался на языке клиента и представился в первом ответе; иначе Нет.
c2 Выявление потребности, Да если учёл детали и уточнил недостающее; N/A если данных хватало сразу.
c3 Идентификация, N/A если запрос не требует её; Да только если подтверждена до защищённого действия.
c4 Консультация, шкала 0,5,10,15,20; N/A только если клиент не сформулировал запрос.
c5 Эмпатия, Да если оператор вежлив и реагирует на конкретное недовольство; иначе Нет.
c6 Ясность, скорость и грамотность, шкала 0,4,7,10.
c7 Завершение, Да если оператор подвёл итог/предложил помощь и попрощался; N/A только если чат оборвался до возможности завершить.
c8 Грубая ошибка, Да если есть оскорбление, обход компании или раскрытие конфиденциальных данных; иначе Нет.
Диалог:\n{turns}"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt")
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=500, do_sample=False, pad_token_id=tokenizer.eos_token_id)
    text = tokenizer.decode(output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    return {"model": model_id, "conversation_id": CHAT_ID, "valid_json": parsed is not None, "response": parsed if parsed is not None else text}


def criterion_probe(args):
    """Check whether a smaller, one-criterion prompt rescues the text model."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    row = record(args.dataset, CHAT_ID)
    turns = "\n".join(f"[{i}] {x['speaker']}: {x['text']}" for i, x in enumerate(row["transcript"]))
    model_id = "Qwen/Qwen2.5-0.5B-Instruct"
    model_path = os.environ.get("CQA_QWEN_MODEL_PATH", model_id)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(model_path, dtype=torch.float32).eval()
    messages = [
        {"role": "system", "content": "Оцени только работу оператора. Ответь ровно одной строкой: Да или Нет, затем номер реплики-доказательства."},
        {"role": "user", "content": "Критерий: оператор поздоровался на языке клиента и представился в первом своём ответе?\n" + turns},
    ]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt")
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=60, do_sample=False, pad_token_id=tokenizer.eos_token_id)
    answer = tokenizer.decode(output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
    return {"model": model_id, "conversation_id": CHAT_ID, "criterion": "contact", "expected": "Да, реплика 9", "response": answer}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["speaker", "emotion", "checklist", "criterion"])
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--audio")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.stage in ("speaker", "emotion") and not args.audio:
        parser.error("--audio is required for voice stages")
    run = {"speaker": speaker_probe, "emotion": emotion_probe, "checklist": checklist_probe, "criterion": criterion_probe}[args.stage]
    result = run(args)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
