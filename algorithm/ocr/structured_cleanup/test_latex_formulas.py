import unittest

from latex_formulas import validate_latex


class LatexFormulasTest(unittest.TestCase):
    def test_valid_math_with_a_piecewise_definition(self):
        text = (r"Vector $\mathbf{x}\in\mathbb{R}^{|\mathcal{E}|}$." + "\n\n$$\n"
                + r"H_{ij}=\begin{cases}1,&\text{if } e_i\in\mathcal{E}_{c_j}\\0,&\text{otherwise}\end{cases}\tag{2}"
                + "\n$$")
        checked = validate_latex(text)
        self.assertEqual((checked["inline_count"], checked["display_count"]), (1, 1))
        self.assertEqual(checked["numbered_equations"], [2])

    def test_rejects_invalid_math_structure_and_unknown_commands(self):
        invalid = [r"$\frac{1}{2$", r"$x", r"$\notARealMacro{x}$",
                   r"$$\begin{cases}1&x\end{array}$$", r"$x\tag{1}$"]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_latex(text)

    def test_rejects_duplicate_equation_numbers(self):
        with self.assertRaises(ValueError):
            validate_latex(r"$$x=1\tag{1}$$" + "\n\n" + r"$$y=2\tag{1}$$")


if __name__ == "__main__":
    unittest.main()
