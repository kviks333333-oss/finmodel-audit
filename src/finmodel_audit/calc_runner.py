"""Isolated, bounded arithmetic interpreter. This is not a Python exec service."""
import ast
import json
import math
import operator
import sys


def evaluate(expression, variables):
    if not isinstance(expression, str) or len(expression) > 2000:
        raise ValueError("Expression must be at most 2000 characters.")
    if not isinstance(variables, dict) or len(variables) > 100:
        raise ValueError("At most 100 numeric variables are supported.")
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Expected a finite number within the calculation limit.")
        return value
    for name, value in variables.items():
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            raise ValueError("Invalid variable name.")
        number(value)
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 150:
        raise ValueError("Expression is too complex.")
    operators = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                 ast.Div: operator.truediv, ast.Mod: operator.mod}
    def visit(node):
        if isinstance(node, ast.Constant):
            return number(node.value)
        if isinstance(node, ast.Name) and node.id in variables:
            return number(variables[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = number(visit(node.left)), number(visit(node.right))
            if type(node.op) in operators:
                return number(operators[type(node.op)](left, right))
            if isinstance(node.op, ast.Pow) and abs(right) <= 12:
                return number(left ** right)
            raise ValueError("Unsupported operator or exponent outside [-12, 12].")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            if not 1 <= len(node.args) <= 100:
                raise ValueError("Invalid argument count.")
            args = [number(visit(arg)) for arg in node.args]
            name = node.func.id
            if name == "sum": result = sum(args)
            elif name == "mean": result = sum(args) / len(args)
            elif name == "min": result = min(args)
            elif name == "max": result = max(args)
            elif name == "abs" and len(args) == 1: result = abs(args[0])
            elif name == "round" and len(args) in (1, 2):
                digits = args[1] if len(args) == 2 else 0
                if type(digits) is not int or abs(digits) > 12:
                    raise ValueError("Rounding precision must be an integer in [-12, 12].")
                result = round(args[0], digits)
            else: raise ValueError("Unsupported function.")
            return number(result)
        raise ValueError("Only numeric arithmetic and the documented functions are allowed.")
    return number(visit(tree.body))


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024,) * 2)
        raw = sys.stdin.read(32769)
        if len(raw) > 32768:
            raise ValueError("Calculation input is too large.")
        request = json.loads(raw)
        value = evaluate(request["expression"], request["variables"])
        print(json.dumps({"value": value}, allow_nan=False))
    except (ValueError, KeyError, SyntaxError, ArithmeticError, TypeError, RecursionError) as exc:
        print(json.dumps({"error": str(exc)[:200]}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
