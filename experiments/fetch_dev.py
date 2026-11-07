"""Fetch the organizer's DEV JSON and one call audio via the guide's embedded link.

The signed URL is read from the local PDF and never written into source control.
The output directory is ignored by Git.
"""

import argparse
import html
import json
import re
import urllib.request
from pathlib import Path

from pypdf import PdfReader


def get_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": "CQA-model-probe"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--guide", required=True, type=Path)
    parser.add_argument("--output", default=Path("data"), type=Path)
    args = parser.parse_args()

    guide = PdfReader(str(args.guide))
    actions = [a.get_object().get("/A") for page in guide.pages for a in (page.get("/Annots") or [])]
    urls = [str(a["/URI"]) for a in actions if a and a.get("/URI")]
    if len(urls) != 1:
        raise RuntimeError(f"Expected one embedded DEV link, found {len(urls)}")

    payload = json.loads(get_bytes(urls[0]))
    if not payload.get("isSuccess"):
        raise RuntimeError("DEV API did not return success")
    records = payload["data"]["records"]
    voice = next(x for x in records if x["conversation_id"] == "p4-ru-003")
    player = get_bytes(voice["audio_url"]).decode("utf-8")
    match = re.search(r'<source\s+src="([^"]+)"', player)
    if not match:
        raise RuntimeError("Audio source not found in player page")
    audio = get_bytes(html.unescape(match.group(1)))
    if not (audio.startswith(b"ID3") or audio[0] == 0xFF):
        raise RuntimeError("Audio response is not an MP3")

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "dev.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (args.output / "p4-ru-003.mp3").write_bytes(audio)
    print(f"Saved {len(records)} records and {len(audio)} audio bytes to {args.output}")


if __name__ == "__main__":
    main()
