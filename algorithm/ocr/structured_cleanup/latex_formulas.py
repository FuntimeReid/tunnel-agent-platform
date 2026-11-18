"""Apply PDF-reviewed LaTeX repairs for 2508.11247v1 and check math syntax."""

import json
from pathlib import Path
import re

from pylatexenc.latexwalker import LatexWalker, LatexWalkerParseError

_RULES = Path(__file__).with_name("latex_rules.json")
_MATH = re.compile(r"\$\$(.*?)\$\$|(?<!\$)\$(?!\$)([^\n$]*?)\$(?!\$)", re.S)
_COMMANDS = {
    "mathcal", "mathbf", "mathbb", "operatorname", "mid", "in", "ldots",
    "subseteq", "tag", "begin", "end", "text", "sum", "max", "cdot",
    "eta", "beta", "alpha", "tilde", "top", "frac", "bigcup", "wedge",
    "left", "right", "times", "{", "}", "\\",
}


def validate_latex(markdown):
    """Check this document's math delimiters, groups, environments and commands."""
    formulas, tags = [], []
    end = 0
    for match in _MATH.finditer(markdown):
        if "$" in markdown[end:match.start()]:
            raise ValueError("Unmatched math delimiter before a formula")
        end = match.end()
        display = match.group(1) is not None
        latex = (match.group(1) if display else match.group(2)).strip()
        if not latex:
            raise ValueError("Empty LaTeX formula")
        commands = set(re.findall(r"\\([A-Za-z]+|.)", latex))
        if commands - _COMMANDS:
            raise ValueError(f"Unexpected LaTeX commands: {sorted(commands - _COMMANDS)}")
        try:
            _, start, length = LatexWalker(latex, tolerant_parsing=False).get_latex_nodes()
        except LatexWalkerParseError as exc:
            raise ValueError(f"Invalid LaTeX structure: {exc}") from exc
        if start + length != len(latex):
            raise ValueError("LaTeX formula was only partially parsed")
        depth = 0
        for char in re.sub(r"\\[{}]", "", latex):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced LaTeX braces")
        if depth:
            raise ValueError("Unbalanced LaTeX braces")
        environments = []
        for command, name in re.findall(r"\\(begin|end)\{([^}]+)\}", latex):
            if name != "cases":
                raise ValueError(f"Unexpected math environment: {name}")
            if command == "begin":
                environments.append(name)
            elif not environments or environments.pop() != name:
                raise ValueError("Mismatched LaTeX environments")
        if environments:
            raise ValueError("Unclosed LaTeX environment")
        numbers = [int(n) for n in re.findall(r"\\tag\{(\d+)\}", latex)]
        if numbers and (not display or len(numbers) != 1):
            raise ValueError("Equation numbers must occur once in display math")
        tags.extend(numbers)
        formulas.append(dict(line=markdown.count("\n", 0, match.start()) + 1,
                             display=display, latex=latex,
                             equation_number=numbers[0] if numbers else None))
    if "$" in markdown[end:]:
        raise ValueError("Unmatched trailing math delimiter")
    if len(tags) != len(set(tags)):
        raise ValueError("Duplicate equation numbers")
    return dict(inline_count=sum(not f["display"] for f in formulas),
                display_count=sum(f["display"] for f in formulas),
                numbered_equations=tags, formulas=formulas,
                checks="strict pylatexenc parse; delimiters, braces, environments and supported AMS math commands",
                latex_compiler_run=False)


def normalize_formulas(markdown, source_sha256):
    rules = json.loads(_RULES.read_text())
    if rules["source_sha256"] != source_sha256:
        raise ValueError("The formula annotations belong to another PDF")
    applied = []
    for index, rule in enumerate(rules["repairs"], start=1):
        count = markdown.count(rule["before"])
        if count != rule["occurrences"]:
            raise ValueError(f"Formula rule {index}: expected {rule['occurrences']} occurrences, found {count}")
        markdown = markdown.replace(rule["before"], rule["after"])
        applied.append(dict(rule=index, occurrences=count, source_page=rule["source_page"], reason=rule["reason"]))
    # Keep sentence punctuation next to the closing inline-math delimiter.
    markdown = re.sub(r"(?<!\$)\$(?!\$)([^\n$]*?)\$(?!\$)[ \t]+([,.;:)])", r"$\1$\2", markdown)
    validation = validate_latex(markdown)
    if validation["numbered_equations"] != list(range(1, 8)) or validation["display_count"] != 7:
        raise ValueError("Expected the seven original numbered display equations")
    validation.update(source_sha256=source_sha256, source_pages=rules["source_pages"],
                      reviewed_original_math_spans=rules["reviewed_original_math_spans"], repairs=applied)
    return markdown, validation
