"""Leakage invariant. SPEC §1.2. WRITTEN BEFORE THE SIMULATOR (§15.8).

  "nothing under src/attribution/, src/detector/ or src/policy.py may import
   environment.yaml, mechanisms.yaml, the true-episode ledger, or any
   ground-truth field."

This must pass on an empty tree (Day 0 acceptance) and keep passing as those
modules appear. It checks three ways in, because an import-graph check alone
is not enough — a module can read a forbidden config by path string without
importing anything.

  1. imported module names
  2. any literal mentioning a forbidden config file or the ledger
  3. any reference to a ground-truth attribute name

RESOLVED Day 3. §1.2 forbade attribution from touching `mechanisms.yaml` while
§8.2 required L2 to derive `mechanism_family` from the source/step/reason
signature that lived there. The file was split: `config/signatures.yaml` holds
the public error taxonomy (what an issuer fault LOOKS like — readable off the
Razorpay dashboard) and `config/mechanisms.yaml` keeps the answers (who is
broken, how hard, how far it spreads). Attribution may read the first and never
the second. `test_attribution_reads_signatures_not_mechanisms` pins the line.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

# SPEC §1.2 verbatim. Authoritative.
PROTECTED = [
    SRC / "attribution",
    SRC / "detector",
    SRC / "policy.py",
]

# NOT in §1.2. Added Day 0 on review: leaking ground truth into the LLM
# narrator would be fatal and invisible, and the seasonal/merge stages sit
# squarely on the observation path. Tracked as an open item in WORKLOG.md;
# tested separately so the spec-mandated invariant stays unambiguous.
PROTECTED_EXTENDED = [
    SRC / "seasonal.py",
    SRC / "incident.py",
    SRC / "explain.py",
    SRC / "execution",
]

FORBIDDEN_MODULES = {"environment", "mechanisms"}
FORBIDDEN_LITERALS = {
    "environment.yaml",
    "mechanisms.yaml",
    "episode_ledger",
    "episodes.jsonl",
    "TrueEpisode",
}
GROUND_TRUTH_ATTRS = {
    "root_cause",
    "true_mechanism",
    "primary_cell",
    "onset_arm",
    "onset_offsets",
    "coupling_beta",
    "efficacy",
    "recovery_probability",
}


def _python_files(targets) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        if t.is_file() and t.suffix == ".py":
            out.append(t)
        elif t.is_dir():
            out.extend(p for p in t.rglob("*.py") if p.name != "__init__.py")
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Docstrings are string constants too, but naming a forbidden config in
    prose that explains you do NOT read it is documentation, not leakage. The
    invariant is about what the code does. Collect them so the literal scan can
    skip them -- narrowing the check to executable strings, not weakening it.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []
    docstrings = _docstring_nodes(tree)

    for node in ast.walk(tree):
        # 1. imports
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in FORBIDDEN_MODULES:
                    bad.append(f"line {node.lineno}: imports {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")
            if set(mod) & FORBIDDEN_MODULES:
                bad.append(f"line {node.lineno}: imports from {node.module}")
            for a in node.names:
                if a.name in FORBIDDEN_LITERALS:
                    bad.append(f"line {node.lineno}: imports name {a.name}")

        # 2. path strings — catches open("config/environment.yaml").
        #    Docstrings are exempt; see _docstring_nodes.
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            for lit in FORBIDDEN_LITERALS:
                if lit in node.value:
                    bad.append(f"line {node.lineno}: literal {lit!r}")

        # 3. ground-truth field access
        elif isinstance(node, ast.Attribute) and node.attr in GROUND_TRUTH_ATTRS:
            bad.append(f"line {node.lineno}: reads ground-truth .{node.attr}")

    return bad


@pytest.mark.parametrize("path", _python_files(PROTECTED), ids=str)
def test_no_ground_truth_leak_spec_1_2(path: Path):
    """SPEC §1.2, authoritative."""
    bad = _violations(path)
    assert not bad, f"LEAKAGE in {path.relative_to(REPO)}:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("path", _python_files(PROTECTED_EXTENDED), ids=str)
def test_no_ground_truth_leak_extended(path: Path):
    """Beyond §1.2. See module docstring and WORKLOG.md."""
    bad = _violations(path)
    assert not bad, f"LEAKAGE in {path.relative_to(REPO)}:\n  " + "\n  ".join(bad)


def test_observables_carry_no_ground_truth():
    """SPEC §1.2: PaymentEvent and TelemetryWindow carry no root_cause field.
    Ground truth lives only in the episode ledger.
    """
    from src.schema import PaymentEvent, TelemetryWindow, TrueEpisode

    for cls in (PaymentEvent, TelemetryWindow):
        fields = set(cls.__dataclass_fields__)
        leaked = fields & GROUND_TRUTH_ATTRS
        assert not leaked, f"{cls.__name__} exposes ground truth: {leaked}"

    # and the ledger genuinely holds it, so the test above is not vacuous
    assert GROUND_TRUTH_ATTRS & set(TrueEpisode.__dataclass_fields__)


def test_protected_paths_are_configured():
    """Guards against the invariant silently going vacuous if src/ is
    restructured and the globs stop matching anything.
    """
    assert PROTECTED, "no protected paths configured"
    if SRC.exists() and any(SRC.rglob("*.py")):
        existing = [p for p in PROTECTED + PROTECTED_EXTENDED if p.exists()]
        # Day 0: src/ holds only schema.py, so nothing protected exists yet.
        # From Day 2 onward this must be non-empty.
        if (SRC / "detector").exists() or (SRC / "attribution").exists():
            assert existing, "protected modules exist but none matched"


def test_attribution_reads_signatures_not_mechanisms():
    """The Day 3 split. Attribution may know what an issuer fault looks like;
    it may not know who is broken. If someone ever moves an affected_set or a
    severity into signatures.yaml, this is the test that should start hurting —
    so it checks the CONTENT of the public file, not just who imports it.
    """
    import yaml
    pub = yaml.safe_load((REPO / "config" / "signatures.yaml").read_text(encoding="utf-8"))
    hidden = yaml.safe_load((REPO / "config" / "mechanisms.yaml").read_text(encoding="utf-8"))

    banned = {"affected_set", "ramp", "beta", "severity", "severity_log_odds",
              "duration_minutes", "onset", "overlap_fraction"}
    for family, sig in pub["families"].items():
        assert not (set(sig) & banned), f"{family} leaks generator detail: {set(sig) & banned}"

    # and the hidden half must still hold them, so the check is not vacuous
    assert {"onset", "severity_log_odds", "duration_minutes"} <= set(hidden)
    for mech in hidden["mechanisms"].values():
        assert "affected_set" in mech
