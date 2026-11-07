"""Статика интерфейса: после слияний веток стили не должны «проглатывать» друг друга.

7.11: при слиянии потерялась закрывающая скобка @media — все стили ниже применялись только
при «уменьшить движение». Тест ловит незакрытые и лишние скобки в каждом CSS-файле.
"""
import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"


def _strip(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', css)


def test_css_braces_balanced():
    for path in sorted(WEB.rglob("*.css")):
        depth = 0
        for n, line in enumerate(_strip(path.read_text(encoding="utf-8")).split("\n"), 1):
            depth += line.count("{") - line.count("}")
            assert depth >= 0, f"{path.name}:{n}: лишняя закрывающая скобка"
        assert depth == 0, f"{path.name}: не закрыто блоков — {depth}"
