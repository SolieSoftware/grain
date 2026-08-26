"""The architecture test, enforced continuously. Milestone 8 proves the boundary
holds once; this keeps it true between milestones."""
import ast
import pathlib
import pytest

ENGINE = pathlib.Path(__file__).parents[2] / "src" / "grain" / "engine"
ADAPTERS = {"cli", "server"}


def _imports(path: pathlib.Path) -> set[str]:
    """Every module name an import statement in this file names.

    `ImportFrom.names` matters as much as `.module`: `from grain import domains`
    puts the offending name in `names`, and a RELATIVE import (`from . import
    cli`, `from .. import domains`) has `module is None` entirely. Recording only
    `.module`, and skipping the node when it was None, meant this test could not
    see any of those forms — the four ways the boundary is most likely to be
    crossed were exactly the four it did not check (defect I4).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                found.add(module)
            for alias in node.names:
                found.add(f"{module}.{alias.name}" if module else alias.name)
    return found


# `rglob`, not `glob`: a file added under `engine/anything/` is engine code and is
# bound by the same rule, but was previously unchecked.
@pytest.mark.parametrize("path", sorted(ENGINE.rglob("*.py")))
def test_engine_never_imports_a_domain(path):
    if path.stem in ADAPTERS:
        return  # adapters may name a default domain; the engine core may not
    assert not any("domains" in imp for imp in _imports(path)), (
        f"{path.name} imports a domain module — domain packs are located by path, "
        f"never imported by name."
    )


@pytest.mark.parametrize("path", sorted(ENGINE.rglob("*.py")))
def test_core_engine_never_imports_an_adapter(path):
    if path.stem in ADAPTERS:
        return
    assert not any(imp.rsplit(".", 1)[-1] in ADAPTERS for imp in _imports(path)), (
        f"{path.name} imports an adapter — the library is the product."
    )
