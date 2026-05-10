"""
Scheduled job factories (S3.10).

Each module exports a single async factory that takes whatever runtime
dependencies the job needs and returns a coroutine factory ready to be
registered with the Scheduler. Keeping the wiring at the call site
(rather than module-level singletons) makes jobs easy to test in
isolation.
"""

from src.engine.jobs.killswitch_sync import make_killswitch_sync_job
from src.engine.jobs.nightly_batch import make_nightly_batch_job
from src.engine.jobs.redis_cleanup import make_redis_cleanup_job
from src.engine.jobs.refresh_markets import make_refresh_markets_job
from src.engine.jobs.refresh_thresholds import make_refresh_thresholds_job

__all__ = [
    "make_killswitch_sync_job",
    "make_nightly_batch_job",
    "make_redis_cleanup_job",
    "make_refresh_markets_job",
    "make_refresh_thresholds_job",
]
