#!/usr/bin/env python3
"""Сборка PDF-разборов из Markdown.

Использование:
    python3 project/design/build.py            # собрать все guides/*.md
    python3 project/design/build.py 01-telefon # собрать один

Зависимости: python-пакет markdown; Node.js + playwright (Chromium).
Результат: project/dist/<имя>.pdf, project/dist/<имя>.html, project/dist/covers/<имя>.png
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

META = {
    "00-lead-magnet": {"badge": "Бесплатно", "kicker": "10 фраз,", "pills": ["<b>10</b> готовых фраз", "примеры диалогов", "для детей 5–16 лет"]},
    "01-telefon": {"badge": "Разбор 1", "kicker": "Что сказать, когда…", "pills": ["<b>15</b> фраз", "план на <b>7</b> дней", "семейный договор"]},
    "02-podrostok": {"badge": "Разбор 2", "kicker": "Что сказать, когда…", "pills": ["<b>15</b> фраз", "план на <b>7</b> дней", "как извиниться"]},
    "03-uroki": {"badge": "Разбор 3", "kicker": "Что сказать, когда…", "pills": ["<b>15</b> фраз", "план на <b>7</b> дней", "стоп-приём"]},
    "zz-test": {"badge": "Разбор", "kicker": "Что сказать, когда…", "pills": ["<b>15</b> фраз", "план на <b>7</b> дней"],
                "no": "Сколько можно сидеть в телефоне!", "yes": "Вижу, игра интересная. Через 10 минут выключаем."},
}
BRAND_LINE = "«Что сказать, когда…»<br>Готовые фразы и план на неделю<br>для трудных ситуаций с детьми"


def split_title(md_text: str):
    """Возвращает (заголовок без префикса, подзаголовок, остальной текст)."""
    lines = md_text.strip().splitlines()
    title, subtitle, start = "", "", 0
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            start = i + 1
            break
    # первая непустая строка после заголовка, не являющаяся заголовком, — подзаголовок
    for j in range(start, len(lines)):
        s = lines[j].strip()
        if not s:
            continue
        if not s.startswith("#"):
            subtitle = s.strip("*_ ")
            start = j + 1
        break
    title = re.sub(r"^Что сказать,? когда\s*(…|\.\.\.)\s*", "", title).strip()
    return title, subtitle, "\n".join(lines[start:])


def sections(html_body: str):
    """Делит HTML на блоки по <h2>, возвращает список (заголовок_текст, html)."""
    parts = re.split(r"(?=<h2[ >])", html_body)
    out = []
    for p in parts:
        m = re.match(r"<h2[^>]*>(.*?)</h2>", p, re.S)
        out.append((re.sub("<[^>]+>", "", m.group(1)).strip() if m else "", p))
    return out


def wrap_h3_blocks(block: str, cls_for):
    """Оборачивает каждый h3-подраздел внутри блока в <div class=...>."""
    pieces = re.split(r"(?=<h3[ >])", block)
    head, rest = pieces[0], pieces[1:]
    out = [head]
    for p in rest:
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
    # выноски: «> **Важно.**» — тёплые, остальные — шалфей
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
            block = f'<section class="cheat">{block}<p class="cheat-note">Распечатайте эту страницу и повесьте на видное место.</p></section>'
        elif t.startswith("об авторе"):
            block = f'<section class="about">{block}</section>'
        elif t.startswith("если не помогло") or t.startswith("когда фраз недостаточно"):
            block = f'<section class="help">{block}</section>'
        out.append(block)
    return "".join(out)


def build_html(slug: str) -> str:
    md_text = (GUIDES / f"{slug}.md").read_text(encoding="utf-8")
    title, subtitle, rest = split_title(md_text)
    # дисклеймер: последний абзац, начинающийся с «Материал носит» / «Дисклеймер», — мелким шрифтом
    body = markdown.markdown(rest, extensions=["tables", "sane_lists", "attr_list"])
    body = re.sub(r"<p>(<em>|<strong>)?(Дисклеймер|Материал|Этот материал)((?:(?!<p>).)*?)</p>\s*$",
                  r'<p class="disclaimer">\1\2\3</p>', body, flags=re.S)
    body = enhance(body)
    meta = META.get(slug, {"badge": "Разбор", "kicker": "Что сказать, когда…", "pills": []})
    pills = "".join(f'<span class="pill">{p}</span>' for p in meta["pills"])
    bubbles = ""
    if meta.get("no") and meta.get("yes"):
        bubbles = (f'<div class="bubbles"><div class="bubble no">{html.escape(meta["no"])}</div>'
                   f'<div class="bubble yes">{html.escape(meta["yes"])}</div></div>')
    cover = f"""
<section class="cover"><div class="blob1"></div><div class="blob2"></div>
 <div class="inner">
  <span class="badge">{meta['badge']}</span>
  <div class="kicker">{html.escape(meta['kicker'])}</div>
  <div class="title">{html.escape(title)}</div>
  <div class="subtitle">{html.escape(subtitle)}</div>
  {bubbles}
  <div class="bottom"><div class="promise">{pills}</div><div class="brand">{BRAND_LINE}</div></div>
 </div></section>"""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{html.escape(meta['kicker'] + ' ' + title)}</title><style>{CSS}</style></head>
<body>{cover}<main>{body}</main></body></html>"""


RENDER_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const jobs = JSON.parse(process.argv[2]);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  for (const j of jobs) {
    await page.goto('file://' + j.html, { waitUntil: 'load' });
    await page.pdf({ path: j.pdf, format: 'A4', printBackground: true, preferCSSPageSize: true,
      displayHeaderFooter: true, headerTemplate: '<span></span>',
      footerTemplate: '<div style="width:100%;font-size:8px;color:#8a9395;padding:0 17mm;display:flex;justify-content:space-between;font-family:Inter,sans-serif"><span>«Что сказать, когда…»</span><span class="pageNumber"></span></div>' });
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
        jobs.append({"html": str(h), "pdf": str(DIST / f"{slug}.pdf"), "png": str(DIST / "covers" / f"{slug}.png")})
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
    slugs = args or sorted(p.stem for p in GUIDES.glob("*.md"))
    main(slugs)
