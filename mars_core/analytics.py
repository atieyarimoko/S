"""Bounded local calculations and CSV/JSON statistics for agent tasks."""

import ast
import csv
import hashlib
import io
import json
import math
import operator
import statistics
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext


MAX_EXPRESSION_LENGTH = 512
MAX_EXPRESSION_NODES = 128
MAX_DATA_BYTES = 1_000_000
MAX_DATA_ROWS = 20_000
MAX_DATA_COLUMNS = 100
MAX_NUMERIC_COLUMNS = 20
MAX_GROUPS = 50
MAX_GROUP_VALUES = 20

_CONSTANTS = {
    "pi": Decimal("3.1415926535897932384626433832795028841971693993751"),
    "e": Decimal("2.7182818284590452353602874713526624977572470936999"),
}
_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class AnalysisError(ValueError):
    """Raised when an expression or dataset exceeds the supported safe subset."""


def _decimal(value):
    if isinstance(value, bool):
        raise AnalysisError("真偽値は数値として扱えません")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise AnalysisError(f"数値に変換できません: {value!r}") from error
    if not result.is_finite():
        raise AnalysisError("NaNや無限大は計算できません")
    return result


def _format_decimal(value):
    value = _decimal(value)
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _function(name, args):
    if name in {"sum", "mean", "median", "pstdev", "stdev", "min", "max"}:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            values = list(args[0])
        else:
            values = args
        if not values:
            raise AnalysisError(f"{name}には1個以上の値が必要です")
        if len(values) > 1000:
            raise AnalysisError("1回の統計計算は1,000個以内の値にしてください")
        values = [_decimal(value) for value in values]
        if name == "sum":
            return sum(values, Decimal(0))
        if name == "mean":
            return statistics.mean(values)
        if name == "median":
            return statistics.median(values)
        if name == "pstdev":
            return statistics.pstdev(values)
        if name == "stdev":
            if len(values) < 2:
                raise AnalysisError("標本標準偏差には2個以上の値が必要です")
            return statistics.stdev(values)
        return min(values) if name == "min" else max(values)

    if name == "abs" and len(args) == 1:
        return abs(_decimal(args[0]))
    if name == "sqrt" and len(args) == 1:
        with localcontext() as context:
            context.prec += 10
            return _decimal(args[0]).sqrt()
    if name in {"ln", "log10", "exp"} and len(args) == 1:
        value = _decimal(args[0])
        if name == "ln":
            return value.ln()
        if name == "log10":
            return value.log10()
        return value.exp()
    if name == "round" and len(args) in {1, 2}:
        value = _decimal(args[0])
        places = 0 if len(args) == 1 else args[1]
        if isinstance(places, bool) or not isinstance(places, int) or abs(places) > 18:
            raise AnalysisError("roundの桁数は-18から18の整数にしてください")
        if len(args) == 1:
            return value.to_integral_value()
        return value.quantize(Decimal(1).scaleb(-places))
    raise AnalysisError(f"利用できない関数または引数です: {name}")


def _evaluate_node(node):
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return _decimal(node.value)
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > 1000:
            raise AnalysisError("数値リストは1,000個以内にしてください")
        return [_evaluate_node(item) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_decimal(_evaluate_node(node.operand)))
    if isinstance(node, ast.BinOp):
        left, right = _evaluate_node(node.left), _evaluate_node(node.right)
        if isinstance(node.op, ast.Pow):
            base, exponent = _decimal(left), _decimal(right)
            if exponent != exponent.to_integral_value() or abs(exponent) > 1000:
                raise AnalysisError("べき乗の指数は-1000から1000の整数にしてください")
            return base ** int(exponent)
        if type(node.op) not in _BINARY_OPERATORS:
            raise AnalysisError("この演算子は利用できません")
        return _BINARY_OPERATORS[type(node.op)](_decimal(left), _decimal(right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        return _function(node.func.id, [_evaluate_node(arg) for arg in node.args])
    raise AnalysisError("式に許可されていない構文が含まれています")


def _evaluate_expression(expression, precision):
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as error:
        raise AnalysisError("数式の構文を読み取れません") from error
    if sum(1 for _ in ast.walk(tree)) > MAX_EXPRESSION_NODES:
        raise AnalysisError(f"数式は{MAX_EXPRESSION_NODES}個以内の構文要素にしてください")
    with localcontext() as context:
        context.prec = precision
        try:
            result = _evaluate_node(tree)
        except (ArithmeticError, InvalidOperation, OverflowError, ValueError) as error:
            if isinstance(error, AnalysisError):
                raise
            raise AnalysisError(f"数式を計算できません: {error}") from error
        result = _decimal(result)
        if result.adjusted() > 10_000 or result.adjusted() < -10_000:
            raise AnalysisError("計算結果の桁数が上限を超えています")
        return +result


def calculate(expression):
    if not isinstance(expression, str) or not expression.strip():
        raise AnalysisError("計算式を指定してください")
    expression = expression.strip()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise AnalysisError(f"計算式は{MAX_EXPRESSION_LENGTH}文字以内にしてください")
    lower_precision = _evaluate_expression(expression, 34)
    result = _evaluate_expression(expression, 50)
    tolerance = max(Decimal(1), abs(result)) * Decimal("1e-32")
    difference = abs(result - lower_precision)
    verified = difference <= tolerance
    return {
        "expression": expression,
        "value": _format_decimal(result),
        "verification": {
            "passed": verified,
            "method": "精度34桁と50桁で独立に再計算し差を照合",
            "lower_precision_value": _format_decimal(lower_precision),
            "absolute_difference": _format_decimal(difference),
            "tolerance": _format_decimal(tolerance),
        },
    }


def _decode_dataset(data, suffix):
    if not isinstance(data, bytes) or len(data) > MAX_DATA_BYTES:
        raise AnalysisError(f"分析対象データは{MAX_DATA_BYTES // 1_000_000} MB以内にしてください")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AnalysisError("CSV/JSONはUTF-8形式で保存してください") from error

    if suffix.lower() == ".csv":
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            columns = reader.fieldnames or []
            if not columns or any(not name or not name.strip() for name in columns):
                raise AnalysisError("CSVには空でない列名が必要です")
            if len(columns) != len(set(columns)):
                raise AnalysisError("CSVの列名が重複しています")
            if len(columns) > MAX_DATA_COLUMNS:
                raise AnalysisError(f"CSVは{MAX_DATA_COLUMNS}列以内にしてください")
            rows = []
            for row_number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise AnalysisError(f"CSVの{row_number}行目に列数の不一致があります")
                if len(rows) >= MAX_DATA_ROWS:
                    raise AnalysisError(f"CSVは{MAX_DATA_ROWS}行以内にしてください")
                rows.append(row)
        except csv.Error as error:
            raise AnalysisError(f"CSVを読み取れません: {error}") from error
        return columns, rows

    if suffix.lower() == ".json":
        try:
            records = json.loads(text, parse_float=Decimal, parse_int=Decimal)
        except json.JSONDecodeError as error:
            raise AnalysisError(f"JSONを読み取れません: {error.msg}") from error
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise AnalysisError("JSON分析の対象はオブジェクト配列にしてください")
        if len(records) > MAX_DATA_ROWS:
            raise AnalysisError(f"JSONは{MAX_DATA_ROWS}行以内にしてください")
        columns = list(dict.fromkeys(key for row in records for key in row))
        if not columns or len(columns) > MAX_DATA_COLUMNS:
            raise AnalysisError(f"JSONは1から{MAX_DATA_COLUMNS}列以内にしてください")
        return columns, records
    raise AnalysisError("分析できる形式はCSVとJSONです")


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        result = _decimal(value)
    except AnalysisError:
        return None
    return result


def _summary(values, missing_count):
    if not values:
        return {"count": 0, "missing_count": missing_count}
    total = sum(values, Decimal(0))
    result = {
        "count": len(values),
        "missing_count": missing_count,
        "sum": _format_decimal(total),
        "mean": _format_decimal(statistics.mean(values)),
        "median": _format_decimal(statistics.median(values)),
        "population_stddev": _format_decimal(statistics.pstdev(values)),
        "min": _format_decimal(min(values)),
        "max": _format_decimal(max(values)),
    }
    if len(values) >= 2:
        result["sample_stddev"] = _format_decimal(statistics.stdev(values))
    return result


def _verify_summary(summary):
    count = summary.get("count", 0)
    if count == 0:
        return summary.get("missing_count", 0) >= 0
    total = _decimal(summary["sum"])
    mean = _decimal(summary["mean"])
    median = _decimal(summary["median"])
    minimum = _decimal(summary["min"])
    maximum = _decimal(summary["max"])
    stddev = _decimal(summary["population_stddev"])
    return (
        abs(mean * count - total) <= max(Decimal(1), abs(total)) * Decimal("1e-30")
        and minimum <= median <= maximum
        and stddev >= 0
        and (count + summary["missing_count"]) >= count
    )


def _linear_trend(rows, x_column, y_column):
    points = []
    for row in rows:
        x_value, y_value = _number(row.get(x_column)), _number(row.get(y_column))
        if x_value is not None and y_value is not None:
            points.append((x_value, y_value))
    if len(points) < 2:
        return {"count": len(points), "status": "insufficient_data"}
    x_mean = statistics.mean(x for x, _ in points)
    y_mean = statistics.mean(y for _, y in points)
    x_variance = sum(((x - x_mean) ** 2 for x, _ in points), Decimal(0))
    y_variance = sum(((y - y_mean) ** 2 for _, y in points), Decimal(0))
    if x_variance == 0:
        return {"count": len(points), "status": "constant_x"}
    covariance = sum(
        ((x - x_mean) * (y - y_mean) for x, y in points),
        Decimal(0),
    )
    slope = covariance / x_variance
    intercept = y_mean - slope * x_mean
    correlation = (
        Decimal(0)
        if y_variance == 0
        else covariance / (x_variance * y_variance).sqrt()
    )
    label = (
        "increasing" if correlation > Decimal("0.1")
        else "decreasing" if correlation < Decimal("-0.1")
        else "weak_or_no_linear_trend"
    )
    return {
        "count": len(points),
        "status": "ok",
        "slope": _format_decimal(slope),
        "intercept": _format_decimal(intercept),
        "correlation": _format_decimal(correlation),
        "trend": label,
    }


def analyze(data, suffix, numeric_columns=None, group_by=None, x_column=None, y_columns=None):
    columns, rows = _decode_dataset(data, suffix)
    if not rows:
        raise AnalysisError("分析対象にデータ行がありません")
    if numeric_columns is not None and (
        not isinstance(numeric_columns, list)
        or len(numeric_columns) > MAX_NUMERIC_COLUMNS
        or len(numeric_columns) != len(set(numeric_columns))
    ):
        raise AnalysisError(f"数値列は重複なしで{MAX_NUMERIC_COLUMNS}列以内に指定してください")
    if group_by is not None and group_by not in columns:
        raise AnalysisError(f"分類列が見つかりません: {group_by}")

    if numeric_columns is None:
        selected = []
        for column in columns:
            values = [_number(row.get(column)) for row in rows]
            if any(value is not None for value in values) and all(
                value is not None or _cell_text(row.get(column)) == ""
                for value, row in zip(values, rows)
            ):
                selected.append(column)
    else:
        selected = numeric_columns
        if any(column not in columns for column in selected):
            raise AnalysisError("指定した数値列が見つかりません")

    if not selected or len(selected) > MAX_NUMERIC_COLUMNS:
        raise AnalysisError(f"分析できる数値列は1から{MAX_NUMERIC_COLUMNS}列です")

    summaries = {}
    values_by_column = {}
    for column in selected:
        values = []
        missing = 0
        for row_number, row in enumerate(rows, start=2):
            raw_value = row.get(column)
            value = _number(raw_value)
            if value is None and _cell_text(raw_value):
                raise AnalysisError(f"{row_number}行目の{column}列は数値ではありません")
            if value is None:
                missing += 1
            else:
                values.append(value)
        values_by_column[column] = values
        summary = _summary(values, missing)
        if not _verify_summary(summary):
            raise AnalysisError(f"{column}列の統計検算に失敗しました")
        summaries[column] = summary

    categorical = {}
    category_columns = [group_by] if group_by else [
        column for column in columns if column not in selected
    ][:10]
    for column in category_columns:
        counts = Counter(_cell_text(row.get(column)) or "(空欄)" for row in rows)
        if group_by and len(counts) > MAX_GROUPS:
            raise AnalysisError(
                f"分類列の種類が{MAX_GROUPS}を超えています。より少ない列で分類してください"
            )
        categorical[column] = {
            "distinct_count": len(counts),
            "value_counts": [
                {"value": value, "count": count}
                for value, count in counts.most_common(MAX_GROUP_VALUES)
            ],
        }

    grouped = None
    if group_by:
        group_values = sorted({_cell_text(row.get(group_by)) or "(空欄)" for row in rows})
        grouped = []
        for group_value in group_values:
            group_rows = [
                row for row in rows
                if (_cell_text(row.get(group_by)) or "(空欄)") == group_value
            ]
            group_summary = {}
            for column in selected:
                numbers = [_number(row.get(column)) for row in group_rows]
                nonmissing = [value for value in numbers if value is not None]
                group_summary[column] = {
                    "count": len(nonmissing),
                    "sum": _format_decimal(sum(nonmissing, Decimal(0))),
                    "mean": _format_decimal(statistics.mean(nonmissing)) if nonmissing else None,
                }
            grouped.append({
                "value": group_value,
                "row_count": len(group_rows),
                "numeric": group_summary,
            })

    trends = None
    if x_column is not None:
        if x_column not in columns:
            raise AnalysisError(f"傾向分析のX列が見つかりません: {x_column}")
        targets = y_columns if y_columns is not None else [column for column in selected if column != x_column]
        if not isinstance(targets, list) or not targets or any(
            column not in columns or column == x_column for column in targets
        ):
            raise AnalysisError("傾向分析のY列を1列以上指定してください")
        if any(column not in selected for column in [x_column, *targets]):
            raise AnalysisError("傾向分析のX/Y列は数値列として指定してください")
        trends = {
            "x_column": x_column,
            "columns": {
                column: _linear_trend(rows, x_column, column)
                for column in targets
            },
        }

    chart = None
    if group_by and grouped:
        maximum = max(group["row_count"] for group in grouped)
        chart = [
            f"{group['value'][:30]} | {'#' * max(1, round(30 * group['row_count'] / maximum))} {group['row_count']}"
            for group in grouped[:MAX_GROUP_VALUES]
        ]

    result = {
        "format": suffix.lower(),
        "row_count": len(rows),
        "columns": columns,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "options": {
            "numeric_columns": numeric_columns,
            "group_by": group_by,
            "x_column": x_column,
            "y_columns": y_columns,
        },
        "numeric": summaries,
        "categorical": categorical,
        "groups": {"column": group_by, "values": grouped} if grouped is not None else None,
        "linear_trends": trends,
        "group_count_chart": chart,
        "verification": {
            "passed": all(_verify_summary(summary) for summary in summaries.values()),
            "checks": ["平均×件数と合計", "中央値の範囲", "標準偏差の非負", "入力SHA-256"],
        },
    }
    return result


def verify_analysis(data, suffix, result, **options):
    if not isinstance(result, dict):
        raise AnalysisError("検証対象の分析結果が不正です")
    if not options:
        options = result.get("options", {})
    if not isinstance(options, dict):
        raise AnalysisError("検証対象の分析条件が不正です")
    recomputed = analyze(data, suffix, **options)
    if (
        result.get("source_sha256") != hashlib.sha256(data).hexdigest()
        or result != recomputed
        or not result.get("verification", {}).get("passed")
    ):
        raise AnalysisError("再分析の結果が保存済み結果と一致しません")
    return {
        "passed": True,
        "source_sha256": result["source_sha256"],
        "row_count": result["row_count"],
        "method": "保存済みの入力ファイルを再読込し、全統計値・分類・傾向を再計算",
    }
