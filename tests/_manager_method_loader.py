"""Helpers for loading clipboard manager code without importing NVDA."""

from __future__ import annotations

import ast
from pathlib import Path


_MANAGER_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "manager.py"


def loadManagerClassMethods(
	methodNames: set[str],
	namespace: dict[str, object] | None = None,
) -> dict[str, object]:
	"""Load selected ClipboardManagerFrame methods without GUI dependencies."""
	tree = ast.parse(_MANAGER_PATH.read_text(encoding="utf-8"))
	managerClass = next(
		node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClipboardManagerFrame"
	)
	methods = [
		node
		for node in managerClass.body
		if isinstance(node, ast.FunctionDef) and node.name in methodNames
	]
	return _execNodes(methods, namespace)


def loadManagerTopLevelFunctions(
	functionNames: set[str],
	namespace: dict[str, object] | None = None,
) -> dict[str, object]:
	"""Load selected manager module functions without GUI dependencies."""
	tree = ast.parse(_MANAGER_PATH.read_text(encoding="utf-8"))
	functions = [
		node
		for node in tree.body
		if isinstance(node, ast.FunctionDef) and node.name in functionNames
	]
	return _execNodes(functions, namespace)


def _execNodes(
	nodes: list[ast.FunctionDef],
	namespace: dict[str, object] | None,
) -> dict[str, object]:
	"""Execute extracted AST nodes in a small caller-provided namespace."""
	namespace = {} if namespace is None else namespace
	exec(compile(ast.Module(body=nodes, type_ignores=[]), str(_MANAGER_PATH), "exec"), namespace)
	return namespace
