"""Count physical source lines and exclude blank, comment and Python docstring lines."""

import ast
import json
from pathlib import Path


def count_python(path):
    """Count nonempty source lines, documenting multiline SQL as implementation."""
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.update(range(first.lineno, first.end_lineno + 1))
    lines = source.splitlines()
    effective = sum(
        bool(line.strip()) and not line.lstrip().startswith("#") and number not in docstrings
        for number, line in enumerate(lines, 1)
    )
    return {"physical": len(lines), "effective": effective}


def count_javascript(path):
    """The authored client uses line comments, with no generated UI code."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    effective = sum(bool(line.strip()) and not line.lstrip().startswith(("//", "/*", "*")) for line in lines)
    return {"physical": len(lines), "effective": effective}


def main():
    """Separate business implementation, tests, and verification tooling."""
    root = Path(__file__).resolve().parents[1]
    files = {}
    for path in sorted((root / "backend").glob("*.py")):
        files[path.relative_to(root).as_posix()] = count_python(path)
    files["run.py"] = count_python(root / "run.py")
    files["frontend/app.js"] = count_javascript(root / "frontend/app.js")
    test_files = {
        p.relative_to(root).as_posix(): count_python(p) for p in sorted((root / "tests").glob("*.py"))
    }
    report = {
        "business_files": files,
        "business_effective_lines": sum(v["effective"] for v in files.values()),
        "test_files": test_files,
        "tests_effective_lines": sum(v["effective"] for v in test_files.values()),
        "excluded": [
            "reused lab2 styles.css",
            "HTML",
            "CSS",
            "documentation",
            "blank lines",
            "comments",
            "Python docstrings",
            "tool scripts",
        ],
        "note": "Physical source-line metric, not statement count; multiline SQL schema is included.",
    }
    output = root / "verification"
    output.mkdir(exist_ok=True)
    (output / "line-count.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
