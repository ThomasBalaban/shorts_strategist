from typing import Any, Callable, Dict, List


def generate_then_critique(
    generate: Callable[[], List[Dict[str, Any]]],
    critique: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    select: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
    trace_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    candidates = generate()
    trace_steps.append({"name": "generate", "candidates": candidates})

    critiqued = critique(candidates)
    trace_steps.append({"name": "critique", "critiqued": critiqued})

    chosen = select(critiqued)
    trace_steps.append({"name": "select", "chosen": chosen})

    return chosen
