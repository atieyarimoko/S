import json
import unittest
from decimal import Decimal

from mars_core.analytics import AnalysisError, analyze, calculate, verify_analysis


class CalculatorTests(unittest.TestCase):
    def test_calculates_with_precedence_statistics_and_decimal_precision(self):
        self.assertEqual(calculate("2 + 3 * 4")["value"], "14")
        self.assertEqual(calculate("mean([1, 2, 8])")["value"], "3.6666666666666666666666666666666666666666666666667")
        self.assertEqual(calculate("sqrt(2) * sqrt(2)")["value"], "2")
        self.assertTrue(calculate("(1 / 3) * 3")["verification"]["passed"])
        self.assertEqual(calculate("pi * 2")["verification"]["passed"], True)

    def test_rejects_code_injection_unsupported_syntax_and_unbounded_expressions(self):
        for expression in (
            "__import__('os').system('echo unsafe')",
            "(1).__class__",
            "[x for x in (1, 2)]",
            "2 ** 1001",
            "1 / 0",
            "nan + 1",
        ):
            with self.subTest(expression=expression), self.assertRaises(AnalysisError):
                calculate(expression)
        with self.assertRaises(AnalysisError):
            calculate("1" * 513)

    def test_statistics_functions_validate_inputs(self):
        self.assertEqual(calculate("median([1, 5, 9])")["value"], "5")
        self.assertEqual(calculate("stdev([2, 2, 2])")["value"], "0")
        with self.assertRaises(AnalysisError):
            calculate("stdev([1])")


class DatasetAnalysisTests(unittest.TestCase):
    def test_analyzes_csv_statistics_groups_trend_and_table_chart(self):
        data = (
            "quarter,region,revenue\n"
            "1,east,10\n"
            "2,east,20\n"
            "3,west,30\n"
            "4,west,40\n"
        ).encode()

        result = analyze(
            data,
            ".csv",
            numeric_columns=["quarter", "revenue"],
            group_by="region",
            x_column="quarter",
            y_columns=["revenue"],
        )

        self.assertEqual(result["row_count"], 4)
        self.assertEqual(result["numeric"]["revenue"]["mean"], "25")
        self.assertEqual(result["numeric"]["revenue"]["median"], "25")
        self.assertEqual(result["numeric"]["revenue"]["population_stddev"], "11.18033988749894848204586834")
        self.assertEqual(result["categorical"]["region"]["value_counts"][0]["count"], 2)
        self.assertEqual(result["groups"]["values"][0]["numeric"]["revenue"]["mean"], "15")
        self.assertEqual(result["linear_trends"]["columns"]["revenue"]["trend"], "increasing")
        self.assertEqual(result["linear_trends"]["columns"]["revenue"]["slope"], "10")
        self.assertEqual(len(result["group_count_chart"]), 2)
        self.assertTrue(result["verification"]["passed"])
        self.assertTrue(verify_analysis(
            data,
            ".csv",
            result,
            numeric_columns=["quarter", "revenue"],
            group_by="region",
            x_column="quarter",
            y_columns=["revenue"],
        )["passed"])

    def test_analyzes_json_records_and_missing_values(self):
        data = json.dumps([
            {"team": "red", "score": 8, "optional": None},
            {"team": "blue", "score": 12, "optional": 3},
            {"team": "red", "score": 4, "optional": 5},
        ]).encode()

        result = analyze(data, ".json", group_by="team")

        self.assertEqual(result["numeric"]["score"]["mean"], "8")
        self.assertEqual(result["numeric"]["optional"]["count"], 2)
        self.assertEqual(result["numeric"]["optional"]["missing_count"], 1)
        self.assertEqual(result["groups"]["values"][1]["numeric"]["score"]["sum"], "12")
        self.assertTrue(verify_analysis(data, ".json", result)["passed"])

    def test_refuses_stale_data_malformed_files_and_unbounded_shapes(self):
        data = b"value\n10\n20\n"
        result = analyze(data, ".csv")
        with self.assertRaises(AnalysisError):
            verify_analysis(data + b"30\n", ".csv", result)
        with self.assertRaises(AnalysisError):
            analyze(b"a,b\n1\n", ".csv")
        with self.assertRaises(AnalysisError):
            analyze(b"{not json}", ".json")
        with self.assertRaises(AnalysisError):
            analyze(b"[]", ".json")
        with self.assertRaises(AnalysisError):
            analyze(b"x\n" + b"1\n" * 20_001, ".csv")

    def test_explicit_numeric_column_errors_instead_of_silently_skipping_invalid_cells(self):
        data = b"value\n12\nunknown\n"
        with self.assertRaisesRegex(AnalysisError, "数値ではありません"):
            analyze(data, ".csv", numeric_columns=["value"])

    def test_regression_rejects_non_numeric_or_missing_columns(self):
        data = b"x,y\n1,4\n2,5\n"
        with self.assertRaisesRegex(AnalysisError, "X列"):
            analyze(data, ".csv", numeric_columns=["x", "y"], x_column="missing", y_columns=["y"])
        with self.assertRaisesRegex(AnalysisError, "Y列"):
            analyze(data, ".csv", numeric_columns=["x", "y"], x_column="x", y_columns=[])


if __name__ == "__main__":
    unittest.main()
