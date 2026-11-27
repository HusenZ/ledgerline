"""Categorization: rule DSL (done), Naive Bayes + layered engine (planned)."""

from .rules import STARTER_RULES, Rule, RuleSyntaxError, context, parse_rule

__all__ = ["STARTER_RULES", "Rule", "RuleSyntaxError", "context", "parse_rule"]
