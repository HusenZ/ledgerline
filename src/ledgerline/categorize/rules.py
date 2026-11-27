"""A tiny rule language for categorization, with a hand-written tokenizer and
recursive-descent parser.

    merchant ~ "starbucks|blue bottle"                      -> Food:Coffee
    description contains "NETFLIX" or merchant = "Spotify"  -> Entertainment:Streaming
    amount < -1000 and merchant ~ "property|rent"           -> Housing:Rent
    not (amount < 0) and description ~ "payroll|salary"     -> Income:Salary
    weekday in (sat, sun) and merchant ~ "bar|pub"          -> Food:Nightlife

Grammar::

    rule    := expr "->" CATEGORY
    expr    := term ("or" term)*
    term    := factor ("and" factor)*
    factor  := "not" factor | "(" expr ")" | compare
    compare := FIELD OP VALUE | FIELD "in" "(" VALUE ("," VALUE)* ")"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Union

FIELDS = {"merchant", "description", "amount", "abs", "account", "weekday", "day", "month", "bank_category"}
NUMERIC_FIELDS = {"amount", "abs", "day", "month"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
OPS = ("~", "contains", "startswith", "=", "!=", ">=", "<=", ">", "<", "in")


class RuleSyntaxError(ValueError):
    def __init__(self, msg: str, text: str, pos: int):
        super().__init__(f"{msg} at column {pos + 1}\n  {text}\n  {' ' * pos}^")
        self.pos = pos


@dataclass
class Token:
    kind: str  # STRING NUMBER WORD OP LPAREN RPAREN COMMA EOF
    value: str
    pos: int


_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<op>!=|>=|<=|=|>|<|~)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)


def tokenize(text: str) -> list[Token]:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise RuleSyntaxError(f"unexpected character {text[pos]!r}", text, pos)
        kind = m.lastgroup
        val = m.group()
        if kind == "string":
            tokens.append(Token("STRING", bytes(val[1:-1], "utf-8").decode("unicode_escape"), pos))
        elif kind == "number":
            tokens.append(Token("NUMBER", val, pos))
        elif kind == "op":
            tokens.append(Token("OP", val, pos))
        elif kind == "word":
            low = val.lower()
            tokens.append(Token("OP" if low in ("contains", "startswith", "in") else "WORD", low if low in (
                "and", "or", "not", "contains", "startswith", "in") or low in FIELDS else val, pos))
        elif kind != "ws":
            tokens.append(Token(kind.upper(), val, pos))
        pos = m.end()
    tokens.append(Token("EOF", "", len(text)))
    return tokens


# ------------------------------------------------------------------ AST nodes
@dataclass
class Compare:
    field: str
    op: str
    value: Any
    matcher: Callable[[Any], bool]

    def eval(self, ctx: dict) -> bool:
        return self.matcher(ctx.get(self.field))

    def __str__(self) -> str:
        v = f"({', '.join(map(str, self.value))})" if isinstance(self.value, list) else repr(self.value)
        return f"{self.field} {self.op} {v}"


@dataclass
class And:
    parts: list["Node"]

    def eval(self, ctx: dict) -> bool:
        return all(p.eval(ctx) for p in self.parts)

    def __str__(self) -> str:
        return "(" + " and ".join(map(str, self.parts)) + ")"


@dataclass
class Or:
    parts: list["Node"]

    def eval(self, ctx: dict) -> bool:
        return any(p.eval(ctx) for p in self.parts)

    def __str__(self) -> str:
        return "(" + " or ".join(map(str, self.parts)) + ")"


@dataclass
class Not:
    inner: "Node"

    def eval(self, ctx: dict) -> bool:
        return not self.inner.eval(ctx)

    def __str__(self) -> str:
        return f"not {self.inner}"


Node = Union[Compare, And, Or, Not]


@dataclass
class Rule:
    condition: Node
    category: str
    text: str
    id: int | None = None
    priority: int = 100

    def matches(self, ctx: dict) -> bool:
        try:
            return self.condition.eval(ctx)
        except (TypeError, ValueError):
            return False


# --------------------------------------------------------------------- parser
class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def take(self, kind: str, value: str | None = None) -> Token:
        t = self.cur
        if t.kind != kind or (value is not None and t.value != value):
            want = value or kind.lower()
            got = t.value or t.kind.lower()
            raise RuleSyntaxError(f"expected {want}, got {got!r}", self.text, t.pos)
        self.i += 1
        return t

    def accept(self, kind: str, value: str | None = None) -> bool:
        if self.cur.kind == kind and (value is None or self.cur.value == value):
            self.i += 1
            return True
        return False

    def parse(self) -> Node:
        node = self.expr()
        if self.cur.kind != "EOF":
            raise RuleSyntaxError(f"unexpected {self.cur.value!r}", self.text, self.cur.pos)
        return node

    def expr(self) -> Node:
        parts = [self.term()]
        while self.accept("WORD", "or"):
            parts.append(self.term())
        return parts[0] if len(parts) == 1 else Or(parts)

    def term(self) -> Node:
        parts = [self.factor()]
        while self.accept("WORD", "and"):
            parts.append(self.factor())
        return parts[0] if len(parts) == 1 else And(parts)

    def factor(self) -> Node:
        if self.accept("WORD", "not"):
            return Not(self.factor())
        if self.accept("LPAREN"):
            node = self.expr()
            self.take("RPAREN")
            return node
        return self.compare()

    def value(self, field: str) -> Any:
        t = self.cur
        if t.kind in ("STRING", "NUMBER", "WORD") and t.value not in ("and", "or", "not"):
            self.i += 1
            if field in NUMERIC_FIELDS:
                try:
                    return Decimal(t.value)
                except InvalidOperation:
                    raise RuleSyntaxError(f"{field} needs a number", self.text, t.pos) from None
            if field == "weekday":
                key = t.value.lower()[:3]
                if key not in WEEKDAYS:
                    raise RuleSyntaxError("weekday must be mon..sun", self.text, t.pos)
                return WEEKDAYS[key]
            return t.value
        raise RuleSyntaxError("expected a value", self.text, t.pos)

    def compare(self) -> Compare:
        ft = self.cur
        if ft.kind != "WORD" or ft.value not in FIELDS:
            raise RuleSyntaxError(f"unknown field {ft.value!r} (fields: {', '.join(sorted(FIELDS))})",
                                  self.text, ft.pos)
        self.i += 1
        field = ft.value
        opt = self.take("OP")
        op = opt.value
        if op == "in":
            self.take("LPAREN")
            values = [self.value(field)]
            while self.accept("COMMA"):
                values.append(self.value(field))
            self.take("RPAREN")
            return Compare(field, op, values, _matcher(field, op, values, self.text, opt.pos))
        val = self.value(field)
        return Compare(field, op, val, _matcher(field, op, val, self.text, opt.pos))


def _matcher(field: str, op: str, val: Any, text: str, pos: int) -> Callable[[Any], bool]:
    numeric = field in NUMERIC_FIELDS or field == "weekday"
    if op == "in":
        if numeric:
            s = set(val)
            return lambda x: x in s
        s = {str(v).lower() for v in val}
        return lambda x: (x or "").lower() in s
    if op in (">", "<", ">=", "<="):
        if not numeric:
            raise RuleSyntaxError(f"'{op}' needs a numeric field", text, pos)
        return {">": lambda x: x is not None and x > val, "<": lambda x: x is not None and x < val,
                ">=": lambda x: x is not None and x >= val, "<=": lambda x: x is not None and x <= val}[op]
    if op in ("=", "!="):
        if numeric:
            eq = lambda x: x == val  # noqa: E731
        else:
            low = str(val).lower()
            eq = lambda x: (x or "").lower() == low  # noqa: E731
        return eq if op == "=" else (lambda x: not eq(x))
    if numeric:
        raise RuleSyntaxError(f"'{op}' needs a text field", text, pos)
    if op == "~":
        try:
            rx = re.compile(str(val), re.IGNORECASE)
        except re.error as e:
            raise RuleSyntaxError(f"bad regex: {e}", text, pos) from None
        return lambda x: bool(rx.search(x or ""))
    if op == "contains":
        low = str(val).lower()
        return lambda x: low in (x or "").lower()
    if op == "startswith":
        low = str(val).lower()
        return lambda x: (x or "").lower().startswith(low)
    raise RuleSyntaxError(f"unknown operator {op}", text, pos)


_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][\w &'/.-]*(?::[\w &'/.-]+)*$")


def parse_rule(text: str, rule_id: int | None = None, priority: int = 100) -> Rule:
    if "->" not in text:
        raise RuleSyntaxError("missing '-> Category'", text, len(text))
    cond_text, category = text.rsplit("->", 1)
    category = category.strip()
    if not _CATEGORY_RE.match(category):
        raise RuleSyntaxError(f"invalid category {category!r}", text, len(cond_text) + 2)
    node = _Parser(cond_text.rstrip()).parse()
    return Rule(node, category, text.strip(), rule_id, priority)


def context(t) -> dict:
    """Evaluation context for a storage Transaction."""
    from ..money import from_minor

    amt = from_minor(t.amount_minor, t.currency)
    return {
        "merchant": t.merchant, "description": t.description, "amount": amt, "abs": abs(amt),
        "account": t.account_name, "weekday": t.date.weekday(), "day": Decimal(t.date.day),
        "month": Decimal(t.date.month), "bank_category": t.bank_category or "",
    }


STARTER_RULES: list[str] = [
    'description ~ "payroll|salary|gehalt|direct dep|paycheck" and amount > 0 -> Income:Salary',
    'description ~ "interest paid|zinsen|interest earned" and amount > 0 -> Income:Interest',
    'description ~ "refund|erstattung|reversal" and amount > 0 -> Income:Refunds',
    'description ~ "\\brent\\b|miete|landlord|property mgmt" and amount < 0 -> Housing:Rent',
    'description ~ "mortgage|hypothek" -> Housing:Mortgage',
    'merchant ~ "starbucks|blue bottle|coffee|cafe|espresso|dunkin|peet" -> Food:Coffee',
    'merchant ~ "whole foods|trader joe|safeway|kroger|rewe|lidl|aldi|edeka|tesco|grocery|supermarket|market" -> Food:Groceries',
    'merchant ~ "uber eats|doordash|grubhub|deliveroo|lieferando" -> Food:Delivery',
    'merchant ~ "pizza|burger|grill|sushi|taco|chipotle|restaurant|kitchen|diner|mcdonald|kfc" -> Food:Restaurants',
    'merchant ~ "^uber$|lyft|taxi|bolt" -> Transport:Rideshare',
    'merchant ~ "shell|chevron|exxon|bp|aral|esso|fuel|gas station" -> Transport:Fuel',
    'merchant ~ "metro|transit|mta|bvg|subway|railway|deutsche bahn|amtrak" -> Transport:Public',
    'merchant ~ "netflix|spotify|hulu|disney|youtube|hbo|prime video" -> Entertainment:Streaming',
    'merchant ~ "amazon|walmart|target|ebay|etsy|ikea" -> Shopping:General',
    'merchant ~ "electric|power|utility|water|stadtwerke|pg&e|con ed" -> Bills:Utilities',
    'merchant ~ "comcast|verizon|at&t|t-mobile|vodafone|telekom|internet|mobile" -> Bills:Phone & Internet',
    'merchant ~ "gym|fitness|yoga|peloton" -> Health:Fitness',
    'merchant ~ "pharmacy|cvs|walgreens|apotheke|clinic|dental|doctor" -> Health:Medical',
    'merchant ~ "insurance|versicherung|geico|allstate" -> Bills:Insurance',
    'description ~ "atm|cash withdrawal|geldautomat" -> Cash',
]
