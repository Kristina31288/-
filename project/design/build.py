#!/usr/bin/env python3
"""Сборка PDF-разборов из Markdown.

Использование:
    python3 design/build.py            # собрать все guides/0*.md
    python3 design/build.py 01-telefon # собрать один

Зависимости: python-пакет markdown; Node.js + playwright (Chromium); интернет для Google Fonts.
Результат: dist/<имя>.pdf, dist/<имя>.html, dist/covers/<имя>.png (копия обложки — в site/img/).
"""
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
GUIDES = ROOT / "guides"
DIST = ROOT / "dist"
CSS = (ROOT / "design" / "guide.css").read_text(encoding="utf-8")
FONTS = ("https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;400;600;700"
         "&family=Great+Vibes&family=Montserrat:wght@400;600;700&display=swap")

# Обложки: иллюстрация (site/img/<img>.jpg) и пара «вместо этого → скажите так»
META = {
    "00-lead-magnet": {"badge": "Бесплатно", "img": "angel-left",
                       "pills": ["<b>10</b> готовых фраз", "примеры диалогов", "для детей 5–16 лет"]},
    "01-telefon": {"badge": "Разбор 1", "img": "angel-right",
                   "pills": ["<b>15</b> фраз", "план на <b>7</b> дней", "семейный договор"],
                   "no": "Сколько можно сидеть в телефоне!",
                   "yes": "Вижу, интересно. Через 10 минут выключаем — поставишь таймер?"},
    "02-podrostok": {"badge": "Разбор 2", "img": "angel-left",
                     "pills": ["<b>15</b> фраз", "план на <b>7</b> дней", "как извиниться"],
                     "no": "Как ты со мной разговариваешь?!",
                     "yes": "Стоп. Таким тоном я не разговариваю. Вернёмся позже."},
    "04-podarok": {"badge": "Подарок", "img": "angel-left",
                   "pills": ["<b>7</b> вечеров", "<b>5</b> фраз", "шпаргалка"]},
    "03-uroki": {"badge": "Разбор 3", "img": "angel-right", "gray": True,
                 "pills": ["<b>15</b> фраз", "план на <b>7</b> дней", "стоп-приём"],
                 "no": "Сколько можно сидеть над одним примером!",
                 "yes": "Отложим этот пример, сделаем следующий и вернёмся."},
}


def split_title(md_text: str):
    """Возвращает (заголовок без «Что сказать, когда…», подзаголовок, остальной текст)."""
    lines = md_text.strip().splitlines()
    title, subtitle, start = "", "", 0
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            start = i + 1
            break
    for j in range(start, len(lines)):
        s = lines[j].strip()
        if not s:
            continue
        if not s.startswith("#") and not s.startswith(">"):
            subtitle = s.strip("*_ ")
            start = j + 1
        break
    title = re.sub(r"^Что сказать,? когда\s*(…|\.\.\.)\s*", "", title).strip()
    return title, subtitle, "\n".join(lines[start:])


def sections(html_body: str):
    parts = re.split(r"(?=<h2[ >])", html_body)
    out = []
    for p in parts:
        m = re.match(r"<h2[^>]*>(.*?)</h2>", p, re.S)
        out.append((re.sub("<[^>]+>", "", m.group(1)).strip() if m else "", p))
    return out


def wrap_h3_blocks(block: str, cls_for):
    pieces = re.split(r"(?=<h3[ >])", block)
    out = [pieces[0]]
    for p in pieces[1:]:
        m = re.match(r"<h3[^>]*>(.*?)</h3>", p, re.S)
        cls = cls_for(re.sub("<[^>]+>", "", m.group(1)).lower()) if m else None
        out.append(f'<div class="{cls}">{p}</div>' if cls else p)
    return "".join(out)


def light_cls(t: str):
    if t.startswith("зел"):
        return "light green"
    if t.startswith("жёл") or t.startswith("жел"):
        return "light amber"
    if t.startswith("крас"):
        return "light red"
    return None


def enhance(body: str) -> str:
    body = re.sub(r"<blockquote>\s*<p><strong>(Важно|Осторожно|Внимание)",
                  r'<blockquote class="warn"><p><strong>\1', body)
    out = []
    for title, block in sections(body):
        t = title.lower()
        if t.startswith("светофор"):
            block = wrap_h3_blocks(block, light_cls)
        elif t.startswith("план на"):
            block = wrap_h3_blocks(block, lambda x: "day" if x.startswith("день") else None)
        elif t.startswith("шпаргалка"):
            block = (f'<section class="cheat">{block}'
                     '<p class="cheat-note">Распечатайте эту страницу и повесьте на видное место.</p></section>')
        elif t.startswith("об авторе"):
            block = f'<section class="about">{block}</section>'
        elif t.startswith("если не помогло") or t.startswith("когда фраз недостаточно"):
            block = f'<section class="help">{block}</section>'
        out.append(block)
    return "".join(out)


def build_html(slug: str) -> str:
    md_text = (GUIDES / f"{slug}.md").read_text(encoding="utf-8")
    title, subtitle, rest = split_title(md_text)
    body = markdown.markdown(rest, extensions=["tables", "sane_lists", "attr_list"])
    body = re.sub(r"<p>(<em>|<strong>)?(Дисклеймер|Материал|Этот материал)((?:(?!<p>).)*?)</p>\s*$",
                  r'<p class="disclaimer">\1\2\3</p>', body, flags=re.S)
    body = enhance(body)
    meta = META.get(slug, {"badge": "Разбор", "img": "angel-left", "pills": []})
    img_src = (ROOT / "site" / "img" / f"{meta['img']}.jpg").as_uri()
    pills = "".join(f'<span class="pill">{p}</span>' for p in meta["pills"])
    bubbles = ""
    if meta.get("no") and meta.get("yes"):
        bubbles = (f'<div class="bubbles"><div class="bubble">{html.escape(meta["no"])}</div>'
                   f'<div class="bubble yes">{html.escape(meta["yes"])}</div></div>')
    logo = ('<div class="logo">Что сказать,<span class="when">когда…</span></div>'
            if slug not in ("00-lead-magnet", "04-podarok") else '<div class="logo">Бесплатно<span class="when">от автора</span></div>')
    gray = " g" if meta.get("gray") else ""
    import os
    art = "" if os.environ.get("NO_ART") else f'<div class="archimg{gray}"><img src="{img_src}" alt=""></div>' 
    cover = f"""
<section class="cover"><div class="inner">
  <span class="badge">{html.escape(meta['badge'])}</span>
  {logo}
  {art}
  <div class="title">{html.escape(title)}</div>
  <div class="subtitle">{html.escape(subtitle)}</div>
  {bubbles}
  <div class="bottom">{pills}</div>
</div></section>"""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Что сказать, когда… {html.escape(title)}</title>
<link href="{FONTS}" rel="stylesheet"><style>{CSS}</style></head>
<body>{cover}<main>{body}</main></body></html>"""


RENDER_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const jobs = JSON.parse(process.argv[2]);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  for (const j of jobs) {
    await page.goto('file://' + j.html, { waitUntil: 'networkidle' });
    await page.evaluate(() => document.fonts.ready);
    await page.pdf({ path: j.pdf, format: 'A4', printBackground: true, preferCSSPageSize: true,
      displayHeaderFooter: true, headerTemplate: '<span></span>',
      footerTemplate: '<div style="width:100%;font-size:8px;color:#8a8380;padding:0 17mm;display:flex;justify-content:space-between;font-family:Montserrat,sans-serif"><span>«Что сказать, когда…»</span><span class="pageNumber"></span></div>' });
    await page.setViewportSize({ width: 794, height: 1123 });
    await page.screenshot({ path: j.png, clip: { x: 0, y: 0, width: 794, height: 1123 } });
  }
  await browser.close();
})();
"""


def main(slugs):
    DIST.mkdir(exist_ok=True)
    (DIST / "covers").mkdir(exist_ok=True)
    jobs = []
    for slug in slugs:
        h = DIST / f"{slug}.html"
        h.write_text(build_html(slug), encoding="utf-8")
        jobs.append({"html": str(h), "pdf": str(DIST / f"{slug}.pdf"),
                     "png": str(DIST / "covers" / f"{slug}.png")})
    js = DIST / "_render.cjs"
    js.write_text(RENDER_JS, encoding="utf-8")
    subprocess.run(["node", str(js), json.dumps(jobs)], check=True)
    js.unlink()
    site_img = ROOT / "site" / "img"
    site_img.mkdir(parents=True, exist_ok=True)
    for j in jobs:
        shutil.copy(j["png"], site_img / Path(j["png"]).name)
        print("OK", j["pdf"])


if __name__ == "__main__":
    args = sys.argv[1:]
    slugs = args or sorted(p.stem for p in GUIDES.glob("0*.md"))
    main(slugs)
