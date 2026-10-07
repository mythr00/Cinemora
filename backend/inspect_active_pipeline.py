from pathlib import Path
import ast

FILES = [
    Path("services/script_analyzer.py"),
    Path("services/media_pipeline.py"),
    Path("jobs.py"),
]

TARGETS = {
    "analyze_script",
    "process_scene_media",
    "_uve_search_candidates",
    "_uve_process_scene_media",
    "build_production_scenes",
    "compose_queries",
    "verify_candidate",
    "plan_script",
}

for path in FILES:
    print()
    print("=" * 110)
    print(f"FILE: {path}")
    print("=" * 110)

    if not path.exists():
        print("FILE NOT FOUND")
        continue

    # utf-8-sig removes BOM if the file contains one
    source = path.read_text(
        encoding="utf-8-sig"
    )

    try:
        tree = ast.parse(source)
    except Exception as e:
        print("AST ERROR:", repr(e))
        continue

    lines = source.splitlines()

    found = set()

    for node in ast.walk(tree):

        if not isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue

        if node.name not in TARGETS:
            continue

        found.add(node.name)

        print()
        print("-" * 110)
        print(f"FUNCTION: {node.name}")
        print(f"LINE: {node.lineno}")
        print(f"END: {getattr(node, 'end_lineno', '?')}")

        positional = [
            a.arg
            for a in node.args.posonlyargs
        ]

        positional += [
            a.arg
            for a in node.args.args
        ]

        keyword_only = [
            a.arg
            for a in node.args.kwonlyargs
        ]

        print("POSITIONAL ARGS:", positional)
        print("KEYWORD-ONLY ARGS:", keyword_only)

        if node.args.vararg:
            print(
                "VARARGS:",
                node.args.vararg.arg
            )

        if node.args.kwarg:
            print(
                "KWARGS:",
                node.args.kwarg.arg
            )

        start = node.lineno - 1
        end = getattr(
            node,
            "end_lineno",
            node.lineno,
        )

        print()
        print("SOURCE:")
        print("-" * 110)

        # Maximum 500 lines per function
        for i in range(
            start,
            min(end, start + 500),
        ):
            print(
                f"{i + 1:5}: {lines[i]}"
            )

        print("-" * 110)

    if not found:
        print(
            "NONE OF THE TARGET FUNCTIONS "
            "WERE FOUND IN THIS FILE."
        )

print()
print("=" * 110)
print("DIAGNOSTIC COMPLETE")
print("=" * 110)
