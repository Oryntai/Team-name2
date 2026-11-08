"""Точная привязка реплик звонка к записи: whisper (локально) + сопоставление текста.

Что делает:
  1. mp3 → wav 16 кГц (ffmpeg, во временную папку) → whisper-cli с метками времени слов (-ojf, -dtw).
     Петли whisper (один текст 3+ раза — на удержании и музыке) выкидываются; пропуски длиннее 4 с
     распознаются ещё раз без переноса контекста (-mc 0); весь звонок с -mc 0 даёт меньше точных реплик.
  2. Слова реплик CRM и слова whisper сопоставляются монотонно (наибольшая общая подпоследовательность
     по началу слова: «обращайтесь» ≈ «обращаетесь»). Порядок реплик сохраняется.
  3. Начало реплики = начало слова whisper, сопоставленного с одним из первых слов реплики (если первые слова
     не совпали — шаг назад на столько же слов whisper, но не на чужую реплику и не через паузу).
     Время слова — по меткам токенов: DTW (флаг --dtw) запаздывает на ~0,3 с относительно начала звука.
     Реплика без уверенного совпадения → null (сервер оставит прежнюю оценку по окнам записи).
  4. Пишет cqa/audio_align.json: {conversation_id: [секунда или null на каждую реплику по индексу]} — только числа.

Запуск (всё локально, $0):
  uv run python lab/audio_align.py                       # распознать заново и собрать
  uv run python lab/audio_align.py --whisper-dir DIR     # кеш распознавания: DIR/<id>.json — основной проход,
                                                         # DIR/<id>.full.json — с дозаполнением пропусков
Нужны: ffmpeg, whisper-cli (brew whisper-cpp), модель ~/.local/share/whisper/ggml-large-v3-turbo-q8_0.bin,
записи data/audio/<id>.mp3, транскрипты data/dev.json.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import audio  # noqa: E402

MODEL = pathlib.Path.home() / ".local/share/whisper/ggml-large-v3-turbo-q8_0.bin"
OUT = ROOT / "cqa" / "audio_align.json"
GAP_S = 4.0        # пропуск whisper длиннее — распознаём кусок ещё раз
HEAD_WORDS = 3     # начало реплики ищем среди первых трёх её слов (до шести, если совпала большая часть)
MAX_SHIFT_S = 60.0  # точная секунда дальше этого от прежней оценки — не верим


# ---------- whisper ----------
def run_whisper(wav: pathlib.Path, out_base: pathlib.Path, offset_ms: int = 0, dur_ms: int = 0,
                no_context: bool = False) -> dict:
    cmd = ["whisper-cli", "-m", str(MODEL), "-l", "ru", "-t", "8", "-np", "-ojf",
           "-dtw", "large.v3.turbo", "-nfa", "-f", str(wav), "-of", str(out_base)]
    if no_context:  # без переноса текста между окнами: на пропущенных кусках whisper иначе зацикливается
        cmd += ["-mc", "0"]
    if offset_ms:
        cmd += ["-ot", str(offset_ms)]
    if dur_ms:
        cmd += ["-d", str(dur_ms)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(out_base.with_suffix(".json").read_text())


def segments(js: dict) -> list[dict]:
    """Сегменты с текстом без петель whisper: короткий текст 3+ раза или фраза из 3+ слов дважды —
    это повтор на тишине/музыке, и метки времени у него поддельные (ровно по секунде)."""
    segs = [s for s in js.get("transcription", []) if norm_words(s.get("text", ""))]
    count: dict[str, int] = {}
    for s in segs:
        k = " ".join(norm_words(s["text"]))
        count[k] = count.get(k, 0) + 1

    def loop(s: dict) -> bool:
        k = " ".join(norm_words(s["text"]))
        return count[k] >= 3 or count[k] >= 2 and len(k.split()) >= 3
    return [s for s in segs if not loop(s)]


def gaps(segs: list[dict], total_s: float) -> list[tuple[float, float]]:
    out, prev = [], 0.0
    for s in sorted(segs, key=lambda s: s["offsets"]["from"]):
        a = s["offsets"]["from"] / 1000
        if a - prev > GAP_S:
            out.append((prev, a))
        prev = max(prev, s["offsets"]["to"] / 1000)
    if total_s - prev > GAP_S:
        out.append((prev, total_s))
    return out


def transcribe(cid: str, mp3: pathlib.Path, keep: pathlib.Path | None) -> list[dict]:
    """Сегменты whisper (с дозаполнением пропусков). Готовый JSON в keep — берём его."""
    if keep and (keep / f"{cid}.full.json").is_file():
        return json.loads((keep / f"{cid}.full.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        wav = pathlib.Path(tmp) / "in.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp3), "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(wav)], check=True)
        total = wav.stat().st_size / 32000
        main_json = keep / f"{cid}.json" if keep else None
        if main_json and main_json.is_file():  # основной проход уже есть — только дозаполняем пропуски
            segs = segments(json.loads(main_json.read_text()))
        else:
            segs = segments(run_whisper(wav, (keep or pathlib.Path(tmp)) / cid))
        for k, (a, b) in enumerate(gaps(segs, total)):
            extra = segments(run_whisper(wav, pathlib.Path(tmp) / f"gap{k}", int(a * 1000), int((b - a) * 1000),
                                         no_context=True))
            for s in extra:  # whisper пишет и за границей куска — оставим только слова внутри пропуска
                s["clip"] = [a - 0.3, b]
            segs += extra
        segs.sort(key=lambda s: s["offsets"]["from"])
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
        (keep / f"{cid}.full.json").write_text(json.dumps(segs, ensure_ascii=False))
    return segs


# ---------- слова ----------
_BRACKETS = re.compile(r"\[[^\]]*\]")
_NONWORD = re.compile(r"[^\w\s]")


def norm_words(text: str) -> list[str]:
    t = _BRACKETS.sub(" ", text or "").lower().replace("ё", "е").replace("-", "")
    return [w for w in _NONWORD.sub(" ", t).replace("_", " ").split() if w]


def key(w: str) -> str:
    return w[:5]


def whisper_words(segs: list[dict]) -> list[dict]:
    """Слова whisper с временем начала: offsets (мс) и DTW (10 мс)."""
    out = []
    for s in segs:
        lo, hi = s.get("clip") or (-1.0, 1e9)
        toks = [t for t in s.get("tokens", []) if not t["text"].startswith("[_") and not t["text"].startswith(" [_")]
        starts = [i for i, t in enumerate(toks) if t["text"].startswith(" ")]
        words = s["text"].split()
        if len(starts) != len(words):  # не разложилось по токенам — время сегмента на все слова
            if lo <= s["offsets"]["from"] / 1000 < hi:
                for w in words:
                    for nw in norm_words(w):
                        out.append({"w": nw, "t": s["offsets"]["from"] / 1000, "dtw": None})
            continue
        for w, i in zip(words, starts):
            tok = toks[i]
            d = tok.get("t_dtw", -1)
            if not lo <= tok["offsets"]["from"] / 1000 < hi:
                continue
            for nw in norm_words(w):
                out.append({"w": nw, "t": tok["offsets"]["from"] / 1000, "dtw": d / 100 if d is not None and d >= 0 else None})
    out.sort(key=lambda w: w["t"])  # сегменты основного прохода и дозаполнения — в один ряд по времени
    return out


def lcs_pairs(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Наибольшая общая подпоследовательность (монотонные пары индексов)."""
    n, m = len(a), len(b)
    prev = [0] * (m + 1)
    rows = [prev]
    for i in range(1, n + 1):
        cur = [0] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = cur[j - 1] if cur[j - 1] >= prev[j] else prev[j]
        rows.append(cur)
        prev = cur
    pairs = []
    i, j = n, m
    while i and j:
        if a[i - 1] == b[j - 1]:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif rows[i - 1][j] >= rows[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def align(transcript: list[dict], ww: list[dict], use_dtw: bool = False) -> tuple[list[float | None], list[dict]]:
    """Секунда начала каждой реплики в записи или None; плюс подробности для отчёта."""
    est = audio.estimate_offsets(transcript)
    tw = []  # (номер реплики, позиция слова в реплике, слово)
    for r, t in enumerate(transcript):
        if t.get("speaker") == "system":
            continue
        for p, w in enumerate(norm_words(t.get("text", ""))):
            tw.append((r, p, w))
    pairs = lcs_pairs([key(w) for _, _, w in tw], [key(x["w"]) for x in ww])
    by_reply: dict[int, list[tuple[int, int]]] = {}
    for i, j in pairs:
        r, p, _ = tw[i]
        by_reply.setdefault(r, []).append((p, j))
    out: list[float | None] = [None] * len(transcript)
    info = []
    last_j = -1  # последнее слово whisper, уже занятое предыдущими репликами
    for r, t in enumerate(transcript):
        n_words = len(norm_words(t.get("text", "")))
        m = by_reply.get(r, [])
        if m:  # совпадения, оторванные по времени от остальных слов реплики, — случайные
            tm = statistics.median(ww[j]["t"] for _, j in m)
            m = [(p, j) for p, j in m if abs(ww[j]["t"] - tm) <= 3.0 + 0.6 * n_words]
        sec, j_start = None, None
        if m:
            p0, j0 = m[0]
            frac = len(m) / n_words
            ok = (n_words <= 2 and len(m) == n_words and p0 == 0
                  or n_words > 2 and len(m) >= 2 and (p0 < HEAD_WORDS and frac >= 0.3 or p0 < 2 * HEAD_WORDS and frac >= 0.5))
            if ok:
                # несовпавшие первые слова реплики: шагаем назад на столько же слов whisper, но не на чужие
                j_start = max(j0 - p0, last_j + 1)
                while j_start < j0 and ww[j0]["t"] - ww[j_start]["t"] > 1.0 + 0.6 * p0:  # через долгую паузу не шагаем
                    j_start += 1
                w = ww[j_start]
                sec = w["dtw"] if use_dtw and w["dtw"] is not None else w["t"]
                if est[r] is not None and abs(sec - est[r]) > MAX_SHIFT_S:
                    sec, j_start = None, None
            last_j = max(last_j, m[-1][1])
        out[r] = round(sec, 2) if sec is not None else None
        info.append({"n": r + 1, "speaker": t.get("speaker"), "words": n_words, "matched": len(m),
                     "sec": out[r], "est": est[r], "whisper_j": j_start})
    return out, info


def load_calls(path: pathlib.Path) -> list[dict]:
    d = json.loads(path.read_text())
    recs = d["data"]["records"] if "data" in d else d
    return [r for r in recs if audio.recording_windows(r["transcript"])]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=pathlib.Path, default=ROOT / "data" / "dev.json")
    ap.add_argument("--audio-dir", type=pathlib.Path, default=ROOT / "data" / "audio")
    ap.add_argument("--whisper-dir", type=pathlib.Path, default=None, help="куда сохранить / откуда взять распознавание")
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    ap.add_argument("--dtw", action="store_true", help="начало слова по DTW вместо меток токенов")
    ap.add_argument("--details", type=pathlib.Path, default=None, help="подробности сопоставления (JSON, с текстом — не в git)")
    args = ap.parse_args()

    result, details = {}, {}
    for rec in sorted(load_calls(args.data), key=lambda r: r["conversation_id"]):
        cid = rec["conversation_id"]
        mp3 = args.audio_dir / f"{cid}.mp3"
        if not mp3.is_file():
            print(f"{cid}: нет записи {mp3}, пропускаю")
            continue
        ww = whisper_words(transcribe(cid, mp3, args.whisper_dir))
        secs, info = align(rec["transcript"], ww, use_dtw=args.dtw)
        result[cid] = secs
        details[cid] = {"info": info, "whisper": ww}
        spoken = sum(1 for t in rec["transcript"] if t.get("speaker") != "system")
        exact = [i for i in info if i["sec"] is not None]
        diffs = [abs(i["est"] - i["sec"]) for i in exact if i["est"] is not None]
        print(f"{cid}: точно {len(exact)} из {spoken} реплик (всего {len(secs)}, системных {len(secs) - spoken}); "
              f"старая оценка отличалась: среднее {statistics.mean(diffs):.1f} с, максимум {max(diffs):.1f} с"
              if diffs else f"{cid}: точных нет")
    args.out.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"→ {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    if args.details:
        args.details.write_text(json.dumps(details, ensure_ascii=False))


if __name__ == "__main__":
    main()
