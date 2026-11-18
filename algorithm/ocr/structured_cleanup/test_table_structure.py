import unittest

from table_structure import html_to_structure, parse_otsl, table_html, table_markdown


class TableStructureTest(unittest.TestCase):
    def test_combined_spans_and_intentional_empty_cell_survive_html(self):
        raw = "<fcel>Group<lcel><fcel>Score<nl><ucel><xcel><fcel>F1<nl><fcel>A<fcel>B<ecel><nl>"
        structure = parse_otsl(raw, header_rows=2)
        self.assertEqual((structure["row_count"], structure["column_count"]), (3, 3))
        group = structure["cells"][0]
        self.assertEqual((group["rowspan"], group["colspan"]), (2, 2))
        self.assertEqual(structure["cells"][-1]["text"], "")
        self.assertEqual(html_to_structure(table_html(structure)), structure)

    def test_content_is_escaped_without_losing_math_subscripts(self):
        structure = parse_otsl("<fcel>Method<fcel>F1<nl><fcel>HGRAG w/o W<sub>p</sub><fcel>76.8<nl>", 1)
        self.assertEqual(structure["cells"][2]["text"], "HGRAG w/o W_p")
        structure["cells"][2]["text"] = "A & B < C"
        self.assertEqual(html_to_structure(table_html(structure)), structure)

    def test_rejects_broken_merge_and_missing_cells(self):
        invalid = [
            "<ucel><nl>",
            "<fcel>A<fcel>B<nl><fcel>C<xcel><nl>",
            "<fcel>A<lcel><nl><ucel><fcel>B<nl>",
            "<fcel>A<fcel>B<nl><fcel>C<nl>",
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_otsl(raw, 1)

    def test_markdown_flattens_headers_and_repeats_category_per_data_row(self):
        raw = ("<fcel>Category<fcel>MuSiQue<lcel><nl><ucel><fcel>EM<fcel>F1<nl>"
               "<fcel>Classic<fcel>20.3<fcel>28.8<nl><ucel><fcel>21.0<fcel>29.0<nl>")
        body = table_markdown(parse_otsl(raw, 2))
        rows = [[c.strip() for c in line.split("|")[1:-1]] for line in body.splitlines()]
        self.assertEqual(rows[0], ["Category", "MuSiQue EM", "MuSiQue F1"])
        self.assertEqual(rows[2:], [["Classic", "20.3", "28.8"], ["Classic", "21.0", "29.0"]])

    def test_markdown_preserves_empty_header_and_escapes_cell_content(self):
        raw = "<ecel><fcel>Value<nl><fcel>A | B &lt; C<fcel>-<nl>"
        body = table_markdown(parse_otsl(raw, 1))
        self.assertEqual(body.splitlines()[0].split("|")[1].strip(), "")
        self.assertIn(r"A \| B &lt; C", body)


if __name__ == "__main__":
    unittest.main()
