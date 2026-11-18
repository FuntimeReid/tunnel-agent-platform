"""Rebuild tables from PaddleOCR-VL cells for Markdown and structure review."""

from html import escape, unescape
import re

from lxml import html

_TOKENS = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl)>")


def _cell_text(value):
    value = re.sub(r"<sub>(.*?)</sub>", r"_\1", value, flags=re.S)
    value = re.sub(r"<sup>(.*?)</sup>", r"^\1", value, flags=re.S)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(unescape(value).split())


def parse_otsl(raw, header_rows):
    tokens = list(_TOKENS.finditer(raw))
    if not tokens or raw[:tokens[0].start()].strip():
        raise ValueError("Expected PaddleOCR-VL OTSL starting with a cell token")
    cells, grid, widths = [], {}, []
    row = column = 0
    for index, token in enumerate(tokens):
        label = token.group(1)
        end = tokens[index + 1].start() if index + 1 < len(tokens) else len(raw)
        text = _cell_text(raw[token.end():end])
        if label == "nl":
            if text or not column:
                raise ValueError("Invalid OTSL row boundary")
            widths.append(column)
            row, column = row + 1, 0
            continue
        if label in {"fcel", "ecel"}:
            if label == "ecel" and text:
                raise ValueError("OTSL empty cell contains text")
            cell = dict(row=row, column=column, rowspan=1, colspan=1,
                        text=text, header=row < header_rows)
            cells.append(cell)
        else:
            if text:
                raise ValueError("OTSL span continuation contains text")
            if label == "lcel":
                cell = grid.get((row, column - 1))
                valid = cell and cell["row"] == row
            elif label == "ucel":
                cell = grid.get((row - 1, column))
                valid = cell and cell["column"] == column
            else:
                cell = grid.get((row - 1, column))
                valid = cell is not None and cell is grid.get((row, column - 1))
            if not valid:
                raise ValueError(f"Invalid OTSL {label} at row {row}, column {column}")
            cell["rowspan"] = max(cell["rowspan"], row - cell["row"] + 1)
            cell["colspan"] = max(cell["colspan"], column - cell["column"] + 1)
        grid[row, column] = cell
        column += 1
    if column:
        widths.append(column)
    if not widths or len(set(widths)) != 1:
        raise ValueError(f"Inconsistent OTSL row widths: {widths}")
    for cell in cells:
        for r in range(cell["row"], cell["row"] + cell["rowspan"]):
            for c in range(cell["column"], cell["column"] + cell["colspan"]):
                if grid.get((r, c)) is not cell:
                    raise ValueError(f"Nonrectangular OTSL merged cell: {cell}")
    return dict(row_count=len(widths), column_count=widths[0], cells=cells)


def html_to_structure(body):
    """Record final corrected HTML with the same explicit cell schema."""
    tree = html.fromstring(body)
    occupied, cells = set(), []
    rows = tree.xpath(".//tr")
    for r, row in enumerate(rows):
        column = 0
        for node in row.xpath("./td|./th"):
            while (r, column) in occupied:
                column += 1
            cell = dict(row=r, column=column, rowspan=int(node.get("rowspan", 1)),
                        colspan=int(node.get("colspan", 1)),
                        text=_cell_text(html.tostring(node, encoding="unicode")), header=node.tag == "th")
            if cell["rowspan"] < 1 or cell["colspan"] < 1 or r + cell["rowspan"] > len(rows):
                raise ValueError(f"Invalid HTML cell span: {cell}")
            for rr in range(r, r + cell["rowspan"]):
                for cc in range(column, column + cell["colspan"]):
                    if (rr, cc) in occupied:
                        raise ValueError(f"Overlapping HTML cells at {rr}, {cc}")
                    occupied.add((rr, cc))
            cells.append(cell)
            column += cell["colspan"]
    width = max((c for r, c in occupied), default=-1) + 1
    if not rows or occupied != {(r, c) for r in range(len(rows)) for c in range(width)}:
        raise ValueError("HTML table has missing cells")
    return dict(row_count=len(rows), column_count=width, cells=cells)


def table_html(structure):
    rows = []
    for row in range(structure["row_count"]):
        cells = []
        for cell in sorted((c for c in structure["cells"] if c["row"] == row), key=lambda c: c["column"]):
            tag = "th" if cell["header"] else "td"
            spans = "".join(f' {key}="{cell[key]}"' for key in ("rowspan", "colspan") if cell[key] > 1)
            cells.append(f"<{tag}{spans}>{escape(cell['text'])}</{tag}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>\n" + "\n".join(rows) + "\n</table>"


def table_markdown(structure):
    grid = [[None] * structure["column_count"] for _ in range(structure["row_count"])]
    for cell in structure["cells"]:
        for row in range(cell["row"], cell["row"] + cell["rowspan"]):
            for column in range(cell["column"], cell["column"] + cell["colspan"]):
                if grid[row][column] is not None:
                    raise ValueError("Cannot render overlapping table cells")
                grid[row][column] = cell
    if any(cell is None for row in grid for cell in row):
        raise ValueError("Cannot render a table with missing cells")
    header_rows = max(c["row"] + c["rowspan"] for c in structure["cells"] if c["header"])
    headers = []
    for column in range(structure["column_count"]):
        parts, previous = [], None
        for row in range(header_rows):
            cell = grid[row][column]
            if cell is not previous and cell["text"]:
                parts.append(cell["text"])
            previous = cell
        headers.append(" ".join(parts))
    values = [headers] + [[cell["text"] for cell in row] for row in grid[header_rows:]]
    values = [[escape(text, quote=False).replace("\\", "\\\\").replace("|", r"\|") for text in row]
              for row in values]
    widths = [max(3, *(len(row[column]) for row in values)) for column in range(structure["column_count"])]

    def render(row):
        return "| " + " | ".join(text.ljust(width) for text, width in zip(row, widths)) + " |"

    return "\n".join([render(values[0]), render(["-" * w for w in widths]), *(render(row) for row in values[1:])])
