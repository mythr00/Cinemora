"""Phase 4a (D-03/C-11) — RapidOCR packaging parity guard.

RapidOCR (rapidocr-onnxruntime) is frame_verification's primary OCR
engine, but it was installed only in the dev venv and absent from
backend/requirements.txt, so every Docker deployment silently lost
OCR — geography checks recorded not_evaluated /
ocr_engine_unavailable while local dev passed. Same parity class as
the psycopg defect (e0bc355). These tests make the break recur
loudly instead:

- requirements.txt must name the full OCR stack, pinned at exactly
  the versions installed in this environment;
- the production module path must be able to construct the real
  OCR engine in this environment;
- when the engine cannot be built, the degradation must be
  EXPLICIT: a recorded reason and a log line, on top of the
  geography record's ocr_engine_unavailable stamp.
"""

import importlib.metadata
import sys
import types
from pathlib import Path

from services import frame_verification as fv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = BACKEND_DIR / "requirements.txt"

# The package the production code imports, plus the transitive
# packages rapidocr-onnxruntime 1.4.4 declares that were not
# already pinned in requirements.txt (onnxruntime, numpy, pillow,
# PyYAML and tqdm were already there).
OCR_PACKAGES = {
    "rapidocr-onnxruntime": "rapidocr_onnxruntime",
    "opencv-python": "cv2",
    "pyclipper": "pyclipper",
    "shapely": "shapely",
    "six": "six",
}


def _requirements_pins():
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        pins[name.strip().lower()] = version.strip()
    return pins


def test_requirements_pin_the_full_ocr_stack():
    pins = _requirements_pins()
    for package in OCR_PACKAGES:
        assert package in pins, (
            f"{package} missing from backend/requirements.txt "
            "(D-03/C-11: OCR would be silently absent from the "
            "shipped image)"
        )


def test_requirements_ocr_pins_match_this_environment():
    pins = _requirements_pins()
    for package in OCR_PACKAGES:
        installed = importlib.metadata.version(package)
        assert pins[package] == installed, (
            f"requirements.txt pins {package}=={pins[package]} but "
            f"this environment has {installed} — pin what is verified"
        )


def test_rapidocr_engine_constructs_in_this_environment():
    # The exact import + construction the production code performs
    # (services.frame_verification._rapidocr_engine). No skip: if
    # the package disappears from the environment, this must fail,
    # not quietly degrade — that silence was the D-03 defect.
    import rapidocr_onnxruntime  # noqa: F401

    engine = fv._rapidocr_engine()
    assert engine is not None
    assert fv._RAPIDOCR_UNAVAILABLE is False


def test_rapidocr_unavailable_is_explicit_not_silent(monkeypatch, capsys):
    class Boom:
        def __init__(self):
            raise RuntimeError("models missing")

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = Boom
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    monkeypatch.setattr(fv, "_RAPIDOCR_ENGINE", None)
    monkeypatch.setattr(fv, "_RAPIDOCR_UNAVAILABLE", False)
    monkeypatch.setattr(fv, "_RAPIDOCR_UNAVAILABLE_REASON", None)

    assert fv._rapidocr_engine() is None
    assert fv._RAPIDOCR_UNAVAILABLE is True
    # Recorded reason, naming the cause...
    assert fv._RAPIDOCR_UNAVAILABLE_REASON is not None
    assert "models missing" in fv._RAPIDOCR_UNAVAILABLE_REASON
    # ...and a log line, so the degradation is visible in host logs.
    out = capsys.readouterr().out
    assert "RapidOCR unavailable" in out
    assert "ocr_engine_unavailable" in out
