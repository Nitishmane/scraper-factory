"""Shared pytest configuration for the deterministic core suite.

These tests exercise pure logic only -- no network, no Bright Data, no Port, no
OpenTelemetry export. `OTEL_SDK_DISABLED=true` is set before any factory module is
imported so that `telemetry.init()` builds no-op providers instead of trying to open OTLP
exporters to a collector that isn't running.
"""
from __future__ import annotations

import os

# Must be set before `factory.telemetry` (imported transitively by every module under
# test) initialises its providers. Setting it here at collection time covers every test.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
