# CQA model probe

Пробный прогон трёх готовых моделей на DEV-наборе организаторов. Это эксперимент, а не готовая система оценки операторов. Результаты и ограничения: [experiments/REPORT.md](experiments/REPORT.md).

## Что проверяется

1. `microsoft/wavlm-base-plus-sv`: голосовые эмбеддинги и группировка коротких фрагментов одного звонка по двум голосам.
2. `superb/wav2vec2-base-superb-er`: общие эмоции в коротких фрагментах того же звонка.
3. `Qwen/Qwen2.5-0.5B-Instruct`: оценка чата по чек-листу.

Модели работают локально. Репозиторий не содержит транскриптов, аудио и подписанной ссылки на API.

## Повторить на Windows

```powershell
$runtime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $runtime -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r experiments\requirements.txt
.\.venv\Scripts\python.exe experiments\fetch_dev.py --guide '..\CQA_Hackathon_Student_Guide_v3 1.pdf'
.\.venv\Scripts\python.exe experiments\model_probe.py speaker --dataset data\dev.json --audio data\p4-ru-003.mp3
.\.venv\Scripts\python.exe experiments\model_probe.py emotion --dataset data\dev.json --audio data\p4-ru-003.mp3
.\.venv\Scripts\python.exe experiments\model_probe.py checklist --dataset data\dev.json
.\.venv\Scripts\python.exe experiments\model_probe.py criterion --dataset data\dev.json
```

Для Qwen веса также доступны в [официальном зеркале ModelScope](https://modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct). Если модель загружена локально, задайте `CQA_QWEN_MODEL_PATH` с путём к каталогу, содержащему `config.json`, файлы токенизатора и `model.safetensors`.

Скрипт `fetch_dev.py` извлекает подписанную ссылку из локального PDF. Папка `data/` исключена из Git. Ссылка может перестать работать после окончания доступа, предоставленного организаторами.
