"""Local, overnight HD content studio for the revenue/social engine.

This package is a thin DISPATCHER over tooling that already exists in the
stack. It does not install or download anything. Each modality handler wires
to the existing local module and degrades gracefully when the underlying tool
(model, binary, ComfyUI server) is absent.

Modalities: image, voice, video, pdf, text.

Design goals (see [[project_nocturnal_agent_team_buildspec]]):
  * $0 — local-first, no outbound calls initiated here.
  * Yield to Operator — every batch checks core.presence_sensor.is_user_active().
  * Queue-driven — overnight worker pulls jobs and routes by modality.

Public API:
  from core.content_studio import dispatch, capabilities, run_job
"""

from __future__ import annotations

from .capabilities import capabilities  # noqa: F401
from .dispatcher import dispatch, run_job  # noqa: F401

__all__ = ["dispatch", "run_job", "capabilities"]
