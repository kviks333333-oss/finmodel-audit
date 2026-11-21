"""Bounded static A1 dependency evidence, never a calculation engine.

Ranges use only populated cells when materialising edges. Their rectangles are
retained so a query about a blank cell can still find formulas referring to it.
Both branches of IF and all explicit range members are potential dependencies.
Dynamic references and other unsupported features are reported as incomplete.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
import re
from typing import Any

from .workbook import Workbook, _address_parts, _column_letters, _column_number

Node = tuple[str, str]
MAX_EDGES = 500_000
MAX_PRECEDENTS = 10_000
_CELL = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
_RANGE = re.compile(rf"(?:{_CELL})(?::(?:{_CELL}))?|\$?[A-Za-z]{{1,3}}:\$?[A-Za-z]{{1,3}}|\$?[1-9][0-9]*:\$?[1-9][0-9]*")
_IDENTIFIER = re.compile(r"(?:[^\W\d]|[_\\])[\w.\\]*", re.UNICODE)
_QUALIFIER = re.compile(r"(?:\[[^\[\]]+\][^\s+*/^&=<>(),;{}\"'\[\]!-]*|[^\s+*/^&=<>(),;{}\"'\[\]!-]+)!")
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_ERROR = re.compile(r"#(?:REF!|DIV/0!|VALUE!|N/A|NAME\?|NUM!|NULL!|SPILL!|CALC!)", re.I)


def normalize_cell(address: str) -> str:
    if not isinstance(address, str) or len(address) > 20:
        raise ValueError("cell must be a valid A1 address")
    address = address.replace("$", "").upper()
    _address_parts(address)
    return address


def parse_range(reference: str) -> tuple[int, int, int, int]:
    """Return inclusive (first column, first row, last column, last row)."""
    if not isinstance(reference, str) or len(reference) > 50:
        raise ValueError("range must be one bounded A1 rectangle")
    reference = reference.replace("$", "").upper()
    if re.fullmatch(r"[A-Z]{1,3}:[A-Z]{1,3}", reference):
        first, last = map(_column_number, reference.split(":"))
        bounds = first, 1, last, 1_048_576
    elif re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", reference):
        first, last = map(int, reference.split(":"))
        bounds = 1, first, 16_384, last
    else:
        parts = reference.split(":")
        if len(parts) not in (1, 2):
            raise ValueError("range must be one A1 rectangle")
        first_col, first_row = _address_parts(parts[0])
        last_col, last_row = _address_parts(parts[-1])
        bounds = first_col, first_row, last_col, last_row
    c1, r1, c2, r2 = bounds
    if not (1 <= c1 <= c2 <= 16_384 and 1 <= r1 <= r2 <= 1_048_576):
        raise ValueError("range is reversed or outside Excel's grid")
    return bounds


def _ref(node: Node) -> dict[str, str]:
    return {"sheet": node[0], "cell": node[1]}


def _quoted(formula: str, position: int) -> tuple[str, int]:
    delimiter = formula[position]
    result = []
    position += 1
    while position < len(formula):
        if formula[position] == delimiter:
            if position + 1 < len(formula) and formula[position + 1] == delimiter:
                result.append(delimiter)
                position += 2
                continue
            return "".join(result), position + 1
        result.append(formula[position])
        position += 1
    raise ValueError("unterminated quoted token")


class DependencyGraph:
    def __init__(self, workbook: Workbook):
        self.workbook = workbook
        self.forward: dict[Node, set[Node]] = defaultdict(set)
        self.reverse: dict[Node, set[Node]] = defaultdict(set)
        self.issues: dict[Node, list[dict[str, str]]] = defaultdict(list)
        self.ranges: dict[str, list[tuple[Node, tuple[int, int, int, int]]]] = defaultdict(list)
        self._sheet_names = {name.casefold(): name for name in workbook.sheets}
        self._sheet_order = list(workbook.sheets)
        self._indices: dict[str, list[tuple[int, int, str]]] = {}
        self._row_indices: dict[str, list[int]] = {}
        self._names: dict[tuple[str | None, str], dict[str, Any]] = {}
        self._edge_count = 0
        self._parse_steps: dict[Node, int] = defaultdict(int)
        for name, sheet in workbook.sheets.items():
            index = sorted((_address_parts(address)[1], _address_parts(address)[0], address) for address in sheet.cells)
            self._indices[name] = index
            self._row_indices[name] = [item[0] for item in index]
        for definition in workbook.defined_names:
            name = str(definition.get("name", ""))
            scope = None
            if "localSheetId" in definition:
                try:
                    scope_id = int(definition["localSheetId"])
                    if not 0 <= scope_id < len(self._sheet_order):
                        continue
                    scope = self._sheet_order[scope_id]
                except (ValueError, TypeError):
                    continue
            self._names[(scope, name.casefold())] = definition
        for name, sheet in workbook.sheets.items():
            special_ranges = []
            for cell in sheet.cells.values():
                if cell.formula_type in ("dataTable", "array") and cell.formula_attributes.get("ref"):
                    try:
                        special_ranges.append((cell.address, cell.formula_type, parse_range(cell.formula_attributes["ref"])))
                    except ValueError:
                        self._issue((name, cell.address), "invalid_special_range", "The array/data-table range is invalid.")
            for cell in sheet.cells.values():
                node = name, cell.address
                column, row = _address_parts(cell.address)
                for anchor, kind, (c1, r1, c2, r2) in special_ranges:
                    if c1 <= column <= c2 and r1 <= row <= r2:
                        self._issue(node, kind, f"{kind} result belongs to {anchor}; native calculation dependencies are not fully resolved.")
                if cell.formula_type == "dataTable" and not any(issue["code"] == "dataTable" for issue in self.issues.get(node, ())):
                    self._issue(node, "dataTable", "What-if table dependencies require a compatible calculation engine.")
                if cell.formula:
                    self._parse(cell.formula, name, node, set())
                elif cell.formula_type and not self.issues.get(node):
                    self._issue(node, "unavailable_formula", "Formula text is unavailable.")
        self._components = self._find_cycles()
        self._cycle_index = {node: component for component in self._components for node in component}

    def _issue(self, node: Node, code: str, detail: str) -> None:
        issue = {"sheet": node[0], "cell": node[1], "code": code, "detail": detail[:400]}
        if issue not in self.issues[node]:
            self.issues[node].append(issue)

    def _add(self, source: Node, target: Node) -> bool:
        if target in self.forward[source]:
            return True
        if len(self.forward[source]) >= MAX_PRECEDENTS or self._edge_count >= MAX_EDGES:
            self._issue(source, "edge_limit", "The static edge budget was reached.")
            return False
        self.forward[source].add(target)
        self.reverse[target].add(source)
        self._edge_count += 1
        return True

    def populated(self, sheet: str, bounds: tuple[int, int, int, int]):
        c1, r1, c2, r2 = bounds
        indices = self._indices[sheet]
        rows = self._row_indices[sheet]
        for row, column, address in indices[bisect_left(rows, r1):bisect_right(rows, r2)]:
            if c1 <= column <= c2:
                yield address

    def _range(self, token: str, sheet: str, source: Node, *, named: bool = False) -> None:
        try:
            bounds = parse_range(token)
        except ValueError:
            self._issue(source, "invalid_reference", f"Reference {token!r} is invalid.")
            return
        if named:
            # Relative defined names are anchored to workbook selection state, which
            # is deliberately not guessed from the formula using the name.
            ends = token.split(":")
            if any(not re.fullmatch(r"\$[A-Za-z]{1,3}\$[1-9][0-9]*|\$[A-Za-z]{1,3}|\$[1-9][0-9]*", end) for end in ends):
                self._issue(source, "relative_defined_name", "A defined name has relative references; its base cell is not resolved.")
                return
        if ":" not in token:
            self._add(source, (sheet, normalize_cell(token)))
            return
        self.ranges[sheet].append((source, bounds))
        for address in self.populated(sheet, bounds):
            if not self._add(source, (sheet, address)):
                self._issue(source, "edge_limit", "A range was only partially materialised because of the static edge budget.")
                break

    def _name(self, token: str, context_sheet: str, source: Node, resolving: set[tuple[str | None, str]]) -> None:
        key = context_sheet, token.casefold()
        if key not in self._names:
            key = None, token.casefold()
        definition = self._names.get(key)
        if not definition:
            self._issue(source, "unresolved_name", f"Defined name {token!r} is not available.")
            return
        if key in resolving or len(resolving) >= 20:
            self._issue(source, "name_cycle", f"Defined name {token!r} is recursive or exceeds the expansion limit.")
            return
        formula = str(definition.get("value", definition.get("text", "")))
        self._parse(formula, context_sheet, source, resolving | {key}, named=True, global_name=key[0] is None)

    def _parse(self, formula: str, context_sheet: str, source: Node, resolving: set[tuple[str | None, str]], *, named: bool = False, global_name: bool = False) -> None:
        if len(formula) > 65_536:
            self._issue(source, "formula_size_limit", "The formula exceeds the parser's character budget.")
            return
        position = 0
        while position < len(formula):
            self._parse_steps[source] += 1
            if self._parse_steps[source] > 10_000:
                self._issue(source, "formula_expansion_limit", "Formula/name expansion exceeded the parser's token budget.")
                return
            char = formula[position]
            if char == '"':
                try:
                    _, position = _quoted(formula, position)
                except ValueError:
                    self._issue(source, "invalid_formula", "An unterminated string prevents complete parsing.")
                    return
                continue
            error = _ERROR.match(formula, position)
            if error:
                if error[0].upper() == "#REF!":
                    self._issue(source, "broken_reference", "The formula contains a #REF! reference.")
                position = error.end()
                continue
            qualifier = None
            start = position
            if char == "'":
                try:
                    qualifier, end = _quoted(formula, position)
                except ValueError:
                    self._issue(source, "invalid_formula", "An unterminated sheet name prevents complete parsing.")
                    return
                if end >= len(formula) or formula[end] != "!":
                    self._issue(source, "invalid_formula", "A quoted identifier is missing its sheet separator.")
                    position = end
                    continue
                position = end + 1
            else:
                match = _QUALIFIER.match(formula, position)
                if match:
                    qualifier = match[0][:-1]
                    position = match.end()
            target_sheet = context_sheet
            skip_reference = False
            if qualifier is not None:
                if "[" in qualifier or "]" in qualifier:
                    self._issue(source, "external_reference", "A referenced external workbook has not been loaded.")
                    skip_reference = True
                elif ":" in qualifier:
                    self._issue(source, "three_dimensional_reference", "References spanning several worksheets are not expanded.")
                    skip_reference = True
                elif qualifier.casefold() not in self._sheet_names:
                    self._issue(source, "missing_sheet", f"Referenced sheet {qualifier!r} is not present.")
                    skip_reference = True
                else:
                    target_sheet = self._sheet_names[qualifier.casefold()]
            match = _RANGE.match(formula, position)
            if match:
                after = formula[match.end():match.end() + 1]
                valid = not after or (not (after.isalnum() or after in "_.\\") and after != "(")
                if valid:
                    name_key = (target_sheet, match[0].casefold())
                    global_key = (None, match[0].casefold())
                    try:
                        parse_range(match[0])
                        valid_range = True
                    except ValueError:
                        valid_range = False
                    if not skip_reference and not valid_range and (name_key in self._names or global_key in self._names):
                        self._name(match[0], target_sheet, source, resolving)
                    elif named and global_name and qualifier is None:
                        self._issue(source, "unqualified_global_name", "A global defined name omits its worksheet; the defining context is not guessed.")
                    elif not skip_reference:
                        self._range(match[0], target_sheet, source, named=named)
                    position = match.end()
                    if position < len(formula) and formula[position] == "#":
                        self._issue(source, "spill_reference", "The dynamic spill extent is not resolved.")
                        position += 1
                    continue
            identifier = _IDENTIFIER.match(formula, position)
            if identifier:
                token = identifier[0]
                position = identifier.end()
                following = position
                while following < len(formula) and formula[following].isspace():
                    following += 1
                if following < len(formula) and formula[following] == "[":
                    self._issue(source, "structured_reference", "Table-column references are not resolved.")
                    position = self._skip_brackets(formula, following)
                elif following < len(formula) and formula[following] == "(":
                    function = token.upper().removeprefix("_XLFN.").removeprefix("_XLWS.")
                    if function in {"INDIRECT", "OFFSET"}:
                        self._issue(source, "dynamic_reference", f"{function} can address cells that are not statically resolved.")
                    elif function in {"LET", "LAMBDA"}:
                        self._issue(source, "formula_local_names", f"{function} local variables are not resolved.")
                elif token.upper() not in {"TRUE", "FALSE"} and not skip_reference:
                    self._name(token, target_sheet, source, resolving)
                continue
            number = _NUMBER.match(formula, position)
            if number:
                position = number.end()
                continue
            if position < len(formula) and formula[position] == "[":
                external_name = re.match(r"\[[0-9]+\](?:[^\W\d]|[_\\])[\w.\\]*", formula[position:])
                if external_name:
                    self._issue(source, "external_reference", "An external defined name has not been loaded.")
                    position += external_name.end()
                else:
                    self._issue(source, "structured_reference", "A bracketed reference is not resolved.")
                    position = self._skip_brackets(formula, position)
                continue
            if qualifier is not None:
                self._issue(source, "unresolved_reference", "A sheet qualifier is not followed by a supported A1 reference or name.")
            position = max(position + 1, start + 1)

    @staticmethod
    def _skip_brackets(formula: str, position: int) -> int:
        level = 0
        while position < len(formula):
            if formula[position] == "[":
                level += 1
            elif formula[position] == "]":
                level -= 1
                if level == 0:
                    return position + 1
            position += 1
        return position

    def _find_cycles(self) -> list[set[Node]]:
        """Iterative Kosaraju avoids recursion limits on long workbook chains."""
        nodes = set(self.forward) | set(self.reverse)
        visited: set[Node] = set()
        order: list[Node] = []
        for start in sorted(nodes):
            if start in visited:
                continue
            stack = [(start, False)]
            while stack:
                node, done = stack.pop()
                if done:
                    order.append(node)
                elif node not in visited:
                    visited.add(node)
                    stack.append((node, True))
                    stack.extend((child, False) for child in self.forward.get(node, ()) if child not in visited)
        visited.clear()
        components = []
        for start in reversed(order):
            if start in visited:
                continue
            component = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.add(node)
                stack.extend(child for child in self.reverse.get(node, ()) if child not in visited)
            if len(component) > 1 or start in self.forward.get(start, ()):
                components.append(component)
        return components

    def _node(self, node: Node) -> dict[str, Any]:
        cell = self.workbook.sheets[node[0]].cells.get(node[1])
        value = cell.value if cell else None
        formula = cell.formula if cell else None
        value_cut = isinstance(value, str) and len(value) > 2000
        formula_cut = formula is not None and len(formula) > 4000
        return {**_ref(node), "present": cell is not None,
                "value": value[:2000] if value_cut else value,
                "formula": formula[:4000] if formula_cut else formula,
                "formula_type": cell.formula_type if cell else None,
                "value_truncated": value_cut, "formula_truncated": formula_cut}

    def _trace(self, sheet: str, cell: str, depth: int, max_nodes: int, direction: str) -> dict[str, Any]:
        if sheet not in self.workbook.sheets:
            raise ValueError("Unknown sheet")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 10:
            raise ValueError("depth must be an integer from 1 to 10")
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not 1 <= max_nodes <= 500:
            raise ValueError("max_nodes must be an integer from 1 to 500")
        root = sheet, normalize_cell(cell)
        visited = {root}
        queue = deque([(root, 0)])
        edges: set[tuple[Node, Node]] = set()
        reasons = set()
        while queue:
            node, level = queue.popleft()
            adjacent = set((self.forward if direction == "precedents" else self.reverse).get(node, ()))
            if direction == "dependents" and node[1] not in self.workbook.sheets[node[0]].cells:
                column, row = _address_parts(node[1])
                adjacent.update(source for source, (c1, r1, c2, r2) in self.ranges[node[0]] if c1 <= column <= c2 and r1 <= row <= r2)
            if level >= depth:
                if adjacent - visited:
                    reasons.add("depth_limit")
                continue
            for other in sorted(adjacent):
                if other not in visited:
                    if len(visited) >= max_nodes:
                        reasons.add("node_limit")
                        continue
                    visited.add(other)
                    queue.append((other, level + 1))
                edges.add((node, other) if direction == "precedents" else (other, node))
        issues = [issue for node in sorted(visited) for issue in self.issues.get(node, ())]
        if issues:
            reasons.add("unsupported_or_unavailable_references")
        if direction == "dependents" and self.issues:
            reasons.add("unresolved_formulas_elsewhere_may_reference_this_cell")
        nodes = [self._node(node) for node in sorted(visited)]
        if any(node["value_truncated"] or node["formula_truncated"] for node in nodes):
            reasons.add("cell_text_limit")
        cycles = [component for component in self._components if component <= visited]
        return {"root": _ref(root), "direction": direction, "depth": depth,
                "nodes": nodes, "edges": [{"from": _ref(a), "to": _ref(b)} for a, b in sorted(edges)],
                "cycles": [[_ref(node) for node in sorted(component)] for component in cycles],
                "cycle_kind": "strongly_connected_components",
                "issues": issues[:100],
                "truncated": bool(reasons & {"depth_limit", "node_limit", "cell_text_limit"}) or len(issues) > 100,
                "completeness": {"complete": not reasons and len(issues) <= 100,
                                 "scope": "static_explicit_populated_references",
                                 "reasons": sorted(reasons | ({"issue_limit"} if len(issues) > 100 else set()))},
                "range_policy": "Range edges include populated cells; explicit single references may point to blank cells. No formulas were recalculated."}

    def precedents(self, sheet: str, cell: str, depth: int = 1, max_nodes: int = 100) -> dict[str, Any]:
        return self._trace(sheet, cell, depth, max_nodes, "precedents")

    def dependents(self, sheet: str, cell: str, depth: int = 1, max_nodes: int = 100) -> dict[str, Any]:
        return self._trace(sheet, cell, depth, max_nodes, "dependents")

    def summary(self) -> dict[str, Any]:
        counts = Counter(issue["code"] for issues in self.issues.values() for issue in issues)
        cycles = [[_ref(node) for node in sorted(component)[:20]] for component in self._components[:10]]
        truncated = len(self._components) > 10 or any(len(component) > 20 for component in self._components[:10])
        return {"node_count": len(set(self.forward) | set(self.reverse)), "edge_count": self._edge_count,
                "formula_cells_with_incomplete_references": len(self.issues),
                "issue_counts": dict(counts), "cycle_group_count": len(self._components),
                "cycles": cycles, "cycle_kind": "strongly_connected_components", "truncated": truncated,
                "completeness": {"complete": not self.issues, "scope": "static_explicit_populated_references",
                                 "reasons": ["unsupported_or_unavailable_references"] if self.issues else []},
                "range_policy": "Only populated range members are materialised. This graph does not evaluate formulas or resolve active conditional branches."}
