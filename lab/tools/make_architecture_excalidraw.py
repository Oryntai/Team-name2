"""Схема архитектуры CQA в стиле владельца: Excalifont, тонкие чёрные линии без заливки,
синие акценты, эллипсы-сущности, человечки-палочки, нумерованные пояснения, большой эллипс вокруг."""
import json, pathlib, random, sys

random.seed(11)
els = []
BLK, BLUE, RED, GRN = "#1e1e1e", "#1971c2", "#e03131", "#2f9e44"
CH = 0.6  # ширина символа Excalifont в долях кегля (оценка)


def rid():
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))


def base(t, x, y, w, h, color=BLK, sw=1, dashed=False):
    return {"id": rid(), "type": t, "x": round(x), "y": round(y), "width": round(w), "height": round(h),
            "strokeColor": color, "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": sw,
            "strokeStyle": "dashed" if dashed else "solid", "roughness": 1, "roundness": None, "opacity": 100}


def text(x, y, s, fs=20, color=BLK, align="left", container=None):
    lines = s.split("\n")
    w, h = max(len(l) for l in lines) * fs * CH, len(lines) * fs * 1.25
    t = base("text", x, y, w, h, color)
    t.update({"text": s, "originalText": s, "fontSize": fs, "fontFamily": 5, "textAlign": align,
              "verticalAlign": "middle" if container else "top", "containerId": container, "lineHeight": 1.25})
    els.append(t)
    return t


def shape(kind, x, y, w, h, s=None, fs=18, color=BLK, sw=1, dashed=False):
    e = base(kind, x, y, w, h, color, sw, dashed)
    els.append(e)
    if s:
        lines = s.split("\n")
        tw, th = max(len(l) for l in lines) * fs * CH, len(lines) * fs * 1.25
        t = text(x + (w - tw) / 2, y + (h - th) / 2, s, fs, color, "center", container=e["id"])
        e["boundElements"] = [{"type": "text", "id": t["id"]}]
    return e


def line(x, y, pts, color=BLK, sw=1, arrow=False, both=False, dashed=False):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    e = base("arrow" if arrow else "line", x, y, max(xs) - min(xs), max(ys) - min(ys), color, sw, dashed)
    e.update({"points": pts, "startArrowhead": "arrow" if both else None, "endArrowhead": "arrow" if arrow else None})
    if arrow:
        e["roundness"] = {"type": 2}
    els.append(e)


def arrow(x1, y1, x2, y2, color=BLK, sw=1, both=False, dashed=False):
    line(x1, y1, [[0, 0], [x2 - x1, y2 - y1]], color, sw, arrow=True, both=both, dashed=dashed)


def stick(x, y, color=BLK):
    """Человечек-палочка: x, y — верх головы."""
    shape("ellipse", x, y, 36, 36, color=color)
    line(x + 18, y + 36, [[0, 0], [0, 60]], color)
    line(x - 6, y + 52, [[0, 0], [48, 0]], color)
    line(x - 2, y + 136, [[0, 0], [20, -40], [40, 0]], color)


# большой эллипс вокруг всего, как на исходном рисунке
shape("ellipse", -320, -385, 1900, 2600)

text(250, 0, "CQA — как это работает", fs=64)
text(250, 95, "Оцениваем операторов поддержки по чек-листу. ИИ ставит оценки, человек может оспорить.")

# 1. источники
text(100, 185, "1. Откуда берём диалоги", fs=28)
shape("ellipse", 100, 235, 260, 100, "API организаторов\n(DEV и контроль)")
shape("ellipse", 420, 235, 220, 100, "файл\nтранскрипта")
shape("ellipse", 700, 235, 260, 100, "звонок:\nрасшифровка + mp3")
for cx in (230, 530, 830):
    arrow(cx, 340, cx, 466, BLUE)

# 2. сервер и проверка
shape("rectangle", 60, 470, 1180, 190)
text(80, 478, "2. Сервер (Python): проверка по чек-листу", fs=24)
steps = ["факты\nиз кода", "присяжные\nHaiku × 5", "арбитр\nSonnet", "проверка\nреплик", "итог\n0–100 кодом"]
xs = [75, 310, 545, 780, 1015]
for i, (x, s) in enumerate(zip(xs, steps)):
    shape("ellipse", x, 525, 200, 110, s)
    if i:
        arrow(xs[i - 1] + 202, 580, x - 2, 580)
shape("ellipse", 296, 508, 466, 146, color=BLUE, sw=2)  # синий овал-выделение
text(300, 672, "↑ тут вся точность: дешёвые ИИ спорят, Sonnet решает", color=BLUE)

# модели
arrow(530, 654, 530, 756, BLUE)
shape("ellipse", 330, 760, 200, 100, "Claude\n(облако)")
shape("ellipse", 600, 760, 220, 100, "своя модель\n(локально)")
arrow(532, 810, 598, 810, both=True)
text(470, 868, "один переключатель", fs=16)
text(880, 768, "Облако — быстро и точно.\nЛокально — данные не уходят\nиз банка, и жюри это любит.")

# как проверяем один диалог
text(100, 930, "Как проверяем один диалог:", fs=24)
text(100, 970, "1) Код сам находит факты: поздоровался ли, подтвердили ли личность, кто закрыл чат.\n"
               "2) Пять дешёвых ИИ-присяжных оценивают каждый пункт и тыкают в реплику-доказательство.\n"
               "3) Где присяжные не согласны — решает Sonnet.\n"
               "4) Код проверяет, что такие реплики правда есть и их сказал оператор.\n"
               "5) Итог по формуле чек-листа считает код, не модель. Грубый косяк — красный флаг.")

# апелляция с человечками
text(100, 1110, "Апелляция", fs=24)
stick(150, 1160)
text(110, 1305, "оператор:\n«не согласен!»", fs=18)
arrow(210, 1215, 395, 1215, BLUE)
shape("ellipse", 400, 1170, 230, 90, "второе мнение\nИИ")
arrow(632, 1215, 745, 1215, BLUE)
stick(760, 1160)
text(715, 1305, "проверяющий:\nрешает и пишет почему", fs=18)
arrow(820, 1215, 975, 1215, BLUE)
shape("ellipse", 980, 1170, 240, 90, "пересчёт итога\n+ история")

# 3. интерфейс
text(100, 1390, "3. Интерфейс (что видит человек)", fs=28)
screens = ["список\nдиалогов", "карточка:\nреплики + плеер", "чек-лист:\nправим пункты", "апелляции", "аналитика\nи журнал"]
for x, s in zip([80, 320, 560, 800, 1040], screens):
    shape("rectangle", x, 1440, 200, 110, s)
text(330, 1560, "красная плашка\n= грубый косяк", fs=16, color=RED)
line(40, 665, [[0, 0], [0, 830]], BLUE, arrow=True)
text(-60, 1060, "REST", fs=16, color=BLUE)

# 4. CRM
arrow(640, 1612, 640, 1660)
shape("ellipse", 400, 1665, 480, 100, "Dynamics 365 —\nвставляем в их CRM")

# стенд для нас
shape("rectangle", 60, 1800, 1180, 120, color=GRN, dashed=True)
text(85, 1820, "Стенд для нас: 60 чатов за ~23 секунды → сравниваем с медным эталоном (судьи Opus)\n"
               "→ видим точность по каждому пункту и языку → правим промпт → снова 23 секунды", color=GRN)

out = pathlib.Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
scene = {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com", "elements": els,
         "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": {}}
out.write_text(json.dumps(scene, ensure_ascii=False, indent=1), encoding="utf-8")
pathlib.Path(sys.argv[2]).write_text(json.dumps({"type": "excalidraw/clipboard", "elements": els, "files": {}},
                                                ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"элементов: {len(els)}; сцена {out.stat().st_size} Б")
