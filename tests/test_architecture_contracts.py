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


def test_integrated_audio_judgment_is_visible_only_to_inference_and_controller():
    """Higher runtime layers transport callbacks without importing judgment semantics."""
    users = {
        path.stem for path in PACKAGE.glob("*.py")
        if "utterance_progress" in _local_imports(path.stem)
    }
    assert users == {"native_audio", "conversation"}
