# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

from typing import NoReturn

import frappe


def throw(msg: str, exc: type[Exception] = frappe.ValidationError, title: str | None = None) -> NoReturn:
	"""`frappe.throw`, which always raises, declared as never returning.

	The framework annotates `frappe.throw` as returning None, so a type checker takes the code
	after a failed check to be reachable. Use this where that code reads a value the check ruled
	out (the parsed tree, the found function) or where the function must otherwise return."""
	frappe.throw(msg, exc, title=title)
	raise exc(msg)  # frappe.throw has raised; this line only tells the checker so
