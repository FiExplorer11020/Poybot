"""Process-supervisor primitives for the split-daemon topology
(Round 6 / The Spine § 3.5).

Ingestion was historically co-located in the engine process. Round 6
splits it across N systemd-supervised processes so a GIL stall in the
engine no longer pauses ingestion. The systemd units live in
``infra/systemd/``; the registry below is what the dashboard / health
check use to enumerate "is daemon X up?".
"""
