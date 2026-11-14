from __future__ import annotations

import io
import hashlib
import os
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _font_path() -> str:
    configured = os.getenv("FONT_PATH")
    candidates = (configured, *FONT_CANDIDATES) if configured else FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "A Cyrillic-capable TTF font is required. Set FONT_PATH to a local font file."
    )


def make_worksheet(plan: dict[str, object]) -> bytes:
    worksheet = plan["focus_task"]["worksheet"]
    font_name = "BrokolinySans"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, _font_path()))

    output = io.BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    pdf.setTitle(str(worksheet["title"]))
    pdf.setFont(font_name, 20)
    pdf.drawString(46, page_height - 54, str(worksheet["title"]))
    pdf.setFont(font_name, 12)
    pdf.drawString(46, page_height - 84, str(worksheet["instructions"]))

    shape = worksheet["shape"]
    count = int(worksheet["count"])
    if worksheet["activity_type"] == "find_differences":
        _draw_difference_rows(pdf, page_width, page_height)
        question_y = page_height - 355
    else:
        columns = min(4, count)
        rows = (count + columns - 1) // columns
        cell_width = (page_width - 92) / columns
        cell_height = min(105, (page_height - 260) / rows)
        for index in range(count):
            column, row = index % columns, index // columns
            center_x = 46 + cell_width * (column + 0.5)
            center_y = page_height - 155 - cell_height * (row + 0.5)
            _draw_shape(pdf, shape, center_x, center_y, 28, index)
        question_y = page_height - 185 - cell_height * rows
    pdf.setFont(font_name, 12)
    for question in worksheet["questions"]:
        pdf.drawString(46, question_y, f"• {question}")
        question_y -= 25
    pdf.setStrokeColorRGB(0.82, 0.86, 0.88)
    pdf.line(46, 40, page_width - 46, 40)
    pdf.setFont(font_name, 9)
    pdf.drawString(46, 25, "Игровое задание для совместного занятия")
    pdf.save()
    return output.getvalue()


def _draw_difference_rows(
    pdf: canvas.Canvas, page_width: float, page_height: float
) -> None:
    labels = ("Ряд 1", "Ряд 2")
    first = ("circle", "square", "triangle", "circle")
    second = ("circle", "square", "triangle", "square")
    cell_width = (page_width - 120) / 4
    pdf.setFont("BrokolinySans", 11)
    for row_index, (label, shapes) in enumerate(zip(labels, (first, second))):
        y = page_height - 175 - row_index * 105
        pdf.drawString(46, y, label)
        for column, shape in enumerate(shapes):
            _draw_shape(pdf, shape, 104 + cell_width * column, y - 28, 22, column)


def _draw_shape(
    pdf: canvas.Canvas, shape: str, x: float, y: float, radius: float, index: int
) -> None:
    pdf.setLineWidth(2)
    pdf.setStrokeColorRGB(0.12, 0.28, 0.42)
    if shape == "circle" or (shape == "mixed" and index % 3 == 0):
        pdf.circle(x, y, radius, stroke=1, fill=0)
    elif shape == "square" or (shape == "mixed" and index % 3 == 1):
        pdf.rect(x - radius, y - radius, radius * 2, radius * 2, stroke=1, fill=0)
    else:
        path = pdf.beginPath()
        path.moveTo(x, y + radius)
        path.lineTo(x - radius, y - radius)
        path.lineTo(x + radius, y - radius)
        path.close()
        pdf.drawPath(path, stroke=1, fill=0)


def make_stimulus(kind: str, *, variation_key: str = "") -> bytes:
    width, height = 960, 540
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    ink = (31, 72, 96)
    colors = ((236, 160, 82), (97, 165, 133), (127, 151, 203), (224, 116, 117))
    seed = hashlib.sha256(variation_key.encode("utf-8")).digest()
    palette_offset = seed[0] % len(colors)
    colors = colors[palette_offset:] + colors[:palette_offset]
    count = (4 + seed[1] % 2) if kind == "counting" else (3 + seed[1] % 2)
    gap = width // (count + 1)
    for index in range(count):
        center_x, center_y = gap * (index + 1), height // 2
        color = colors[index % len(colors)]
        if kind == "animals":
            draw.ellipse((center_x - 60, center_y - 40, center_x + 60, center_y + 55), fill=color, outline=ink, width=5)
            draw.ellipse((center_x - 42, center_y - 74, center_x - 8, center_y - 30), fill=color, outline=ink, width=5)
            draw.ellipse((center_x + 8, center_y - 74, center_x + 42, center_y - 30), fill=color, outline=ink, width=5)
            draw.ellipse((center_x - 24, center_y - 5, center_x - 14, center_y + 5), fill=ink)
            draw.ellipse((center_x + 14, center_y - 5, center_x + 24, center_y + 5), fill=ink)
        elif kind == "pattern" and (index + seed[2]) % 2:
            draw.rectangle((center_x - 48, center_y - 48, center_x + 48, center_y + 48), fill=color, outline=ink, width=5)
        elif kind == "shapes" and (index + seed[3]) % 2:
            draw.polygon(
                ((center_x, center_y - 60), (center_x - 58, center_y + 48), (center_x + 58, center_y + 48)),
                fill=color,
                outline=ink,
            )
        else:
            draw.ellipse((center_x - 50, center_y - 50, center_x + 50, center_y + 50), fill=color, outline=ink, width=5)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
