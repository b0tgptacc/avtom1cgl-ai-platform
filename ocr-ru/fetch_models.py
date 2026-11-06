"""Скачивает модели Tesseract при сборке образа и сверяет sha256 (версия 4.1.0 репозиториев tesseract-ocr).
Китайский — из tessdata_best: на сканах договоров заметно точнее и быстрее стандартной модели."""
import hashlib
import os
import urllib.request

DEST = os.environ.get("TESSDATA_PREFIX", "/usr/share/tessdata/")
MODELS = {
    "rus": ("tessdata", "681be2c2bead1bc7bd235df88c44e8e60ae73ae866840c0ad4e3b4c247bd37c2"),
    "eng": ("tessdata", "daa0c97d651c19fba3b25e81317cd697e9908c8208090c94c3905381c23fc047"),
    "osd": ("tessdata", "e19f2ae860792fdf372cf48d8ce70ae5da3c4052962fe22e9de1f680c374bb0e"),
    "chi_sim": ("tessdata_best", "4fef2d1306c8e87616d4d3e4c6c67faf5d44be3342290cf8f2f0f6e3aa7e735b"),
}

os.makedirs(DEST, exist_ok=True)
for lang, (repo, sha) in MODELS.items():
    url = f"https://github.com/tesseract-ocr/{repo}/raw/4.1.0/{lang}.traineddata"
    data = urllib.request.urlopen(url, timeout=300).read()
    if hashlib.sha256(data).hexdigest() != sha:
        raise SystemExit(f"sha256 не совпал: {lang}")
    with open(os.path.join(DEST, f"{lang}.traineddata"), "wb") as f:
        f.write(data)
    print(f"{lang}: {len(data) // 1024 // 1024} МБ, sha256 ок")
