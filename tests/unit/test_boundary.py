"""The architecture test, enforced continuously. Milestone 8 proves the boundary
holds once; this keeps it true between milestones."""
import ast
import pathlib
import pytest

ENGINE = pathlib.Path(__file__).parents[2] / "src" / "grain" / "engine"
ADAPTERS = {"cli", "server"}


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")))
def test_engine_never_imports_a_domain(path):
    if path.stem in ADAPTERS:
        return  # adapters may name a default domain; the engine core may not
    assert not any("domains" in imp for imp in _imports(path)), (
        f"{path.name} imports a domain module — domain packs are located by path, "
        f"never imported by name."
    )


@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")))
def test_core_engine_never_imports_an_adapter(path):
    if path.stem in ADAPTERS:
        return
    assert not any(imp.rsplit(".", 1)[-1] in ADAPTERS for imp in _imports(path)), (
        f"{path.name} imports an adapter — the library is the product."
    )
