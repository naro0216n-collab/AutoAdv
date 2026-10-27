"""Architecture contracts for observation, inference, policy, and advancement ownership.

These tests protect product-level state ownership rather than a particular audio
algorithm.  Measurement may change, and the integrated inference may change, but
those changes must not create a second progression policy in an adjacent layer.
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "autoadv"


def _tree(module: str) -> ast.Module:
    return ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))


def _local_imports(module: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(module)):
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        imported.add(node.module.split(".", 1)[0])
    return imported


def _imported_names(module: str, source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(module)):
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module != source:
            continue
        names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_audio_layers_have_no_back_edges_into_policy_or_application():
    """Lower layers cannot import a higher layer to preserve their own purity."""
    forbidden = {
        "audio_evidence": {"acoustic_observers", "utterance_progress", "native_audio", "conversation", "application"},
        "acoustic_observers": {"utterance_progress", "native_audio", "conversation", "application"},
        "utterance_progress": {"native_audio", "conversation", "application"},
        "native_audio": {"conversation", "application"},
        # Controller may consume the integrated judgment, but not reconstruct it
        # from measurement or reach into the runtime adapter that produced it.
        "conversation": {"audio_evidence", "acoustic_observers", "native_audio", "application"},
    }
    violations = {
        module: sorted(_local_imports(module) & disallowed)
        for module, disallowed in forbidden.items()
        if _local_imports(module) & disallowed
    }
    assert not violations, violations


def test_integrated_audio_inference_has_one_product_entrypoint():
    """Only NativeAudio may invoke the measurement-to-progress inference."""
    users: list[str] = []
    for path in PACKAGE.glob("*.py"):
        module = path.stem
        if "evaluate_utterance_progress" in _imported_names(module, "utterance_progress"):
            users.append(module)
    assert set(users) == {"native_audio"}


def test_progress_state_is_not_reinterpreted_outside_its_owner():
    """ACTIVE/ENDED/UNKNOWN semantics belong to inference and Controller only.

    Runtime/application code may transport the integrated result and expose its
    state for telemetry, but it must not import the enum and rebuild a second
    completion rule from those labels.
    """
    enum_users = []
    for path in PACKAGE.glob("*.py"):
        module = path.stem
        if module == "utterance_progress":
            continue
        if "UtteranceProgressState" in _imported_names(module, "utterance_progress"):
            enum_users.append(module)
    assert not enum_users, enum_users

    semantic_reads: dict[str, list[tuple[int, str]]] = {}
    for path in PACKAGE.glob("*.py"):
        module = path.stem
        if module in {"utterance_progress", "conversation"}:
            continue
        tree = _tree(module)
        progress_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    annotation = arg.annotation
                    if isinstance(annotation, ast.Name) and annotation.id == "UtteranceProgress":
                        progress_names.add(arg.arg)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name) and node.value.func.id == "UtteranceProgress":
                    progress_names.update(
                        target.id for target in node.targets if isinstance(target, ast.Name))

        reads: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in {"active", "ended", "unknown"}:
                continue
            if isinstance(node.value, ast.Name) and node.value.id in progress_names:
                reads.append((node.lineno, node.attr))
                continue
            if isinstance(node.value, ast.Attribute) and node.value.attr == "native_progress":
                reads.append((node.lineno, node.attr))
        if reads:
            semantic_reads[module] = reads
    assert not semantic_reads, semantic_reads


def test_application_transports_integrated_audio_without_owning_measurement_or_state_creation():
    """Application may transport progress/diagnostics but not recreate audio inference."""
    assert "audio_evidence" not in _local_imports("application")

    constructors = []
    for node in ast.walk(_tree("application")):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "UtteranceProgress":
            constructors.append(node.lineno)
    assert not constructors, constructors
