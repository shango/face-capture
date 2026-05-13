"""Pipeline package for the face-capture web service.

The orchestrator (`orchestrator.run_pipeline`) is the public entry point. The
individual steps (`crop`, `capture`, `smooth`, `resample`, `preview_overlay`)
remain CLI scripts because the orchestrator invokes them via subprocess to
keep MediaPipe initializations isolated per step.
"""

from .orchestrator import run_pipeline, make_zip

__all__ = ["run_pipeline", "make_zip"]
