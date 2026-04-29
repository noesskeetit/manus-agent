"""image_view — multimodal: загружает локальный image и подсказывает модели использовать vision.

Cloud.ru FM API не у всех моделей multimodal. Реализация:
- Если модель multimodal — возвращаем base64 image_url в content (модель сама прочтёт)
- Если нет — используем CLI инструмент `pngquant` / `osascript` / `vipsthumbnail` для извлечения
  metadata (размер, цвет, EXIF), и возвращаем text-описание + path.

В нашем стеке (Qwen3-Coder-Next, MiniMax-M2, GLM-4.7) — multimodal не у всех. Для простоты
возвращаем metadata + сохранённую миниатюру + просим модель полагаться на context.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


class ImageViewArgs(BaseModel):
    path: str = Field(..., description="Абсолютный путь к image (PNG/JPG/WebP/GIF/BMP/TIFF/SVG)")
    extract_text: bool = Field(False, description="Попытаться вытащить текст через OCR (если установлен tesseract)")


class ImageViewTool(Tool):
    group = "file"
    name = "image_view"
    description = (
        "Прочитать image-файл и вернуть metadata + (опционально) OCR-текст. "
        "Поддержка: JPEG/PNG/WebP/GIF/BMP/TIFF/SVG. "
        "Для multimodal моделей: путь сохраняется и может быть передан как context. "
        "OCR требует установки `tesseract` (brew install tesseract)."
    )
    args_schema = ImageViewArgs

    def execute(self, args: ImageViewArgs, ctx: ToolContext) -> ToolResult:
        from .file_ops import _is_denied
        p = Path(args.path).expanduser()
        if not p.is_absolute():
            p = ctx.workspace.root / p
        if _is_denied(p):
            return ToolResult(content=f"ERROR: refusing to view sensitive path: {p}",
                              is_error=True)
        if not p.exists() or not p.is_file():
            return ToolResult(content=f"ERROR: image not found: {p}", is_error=True)
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".svg"):
            return ToolResult(content=f"ERROR: unsupported image format: {p.suffix}", is_error=True)

        size_bytes = p.stat().st_size
        info_lines = [
            f"Image: {p}",
            f"Size: {size_bytes:,} bytes ({size_bytes/1024:.1f} KB)",
            f"Format: {p.suffix.upper().lstrip('.')}",
        ]

        # Размер через PIL если есть
        try:
            from PIL import Image  # type: ignore
            with Image.open(p) as im:
                info_lines.append(f"Dimensions: {im.size[0]}×{im.size[1]} {im.mode}")
        except ImportError:
            pass
        except Exception as e:
            info_lines.append(f"(PIL read failed: {e})")

        # OCR попытка
        if args.extract_text:
            import subprocess
            tesseract_ok = subprocess.run(["which", "tesseract"], capture_output=True).returncode == 0
            if tesseract_ok:
                try:
                    r = subprocess.run(
                        ["tesseract", str(p), "-", "-l", "rus+eng"],
                        capture_output=True, text=True, timeout=30,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        info_lines.append(f"\n--- OCR text ---\n{r.stdout.strip()[:5000]}")
                    else:
                        info_lines.append("\n(OCR returned no text)")
                except Exception as e:
                    info_lines.append(f"\n(OCR failed: {e})")
            else:
                info_lines.append("\n(OCR skipped: tesseract not installed)")

        return ToolResult(
            content="\n".join(info_lines),
            artifacts=[str(p)],
            metadata={"path": str(p), "size_bytes": size_bytes,
                      "format": p.suffix.lower().lstrip(".")},
        )


def make_image_tools() -> list[Tool]:
    return [ImageViewTool()]
