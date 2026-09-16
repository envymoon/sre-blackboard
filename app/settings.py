"""External-service config: reach everything over the network, fall back to local stubs
for whatever is missing. Maps to spec Sec.28.3: models / MySQL / Redis / ES are all external.

Empty MYSQL_URL/REDIS_URL/ES_URL means local: in-memory sqlite + in-memory queue + in-memory knowledge index.
The reranker stays base-model + rules, no fine-tune (release gate in Sec.12.8.2 not met yet).
"""
from __future__ import annotations
import os

MYSQL_URL = os.getenv("MYSQL_URL", "")
REDIS_URL = os.getenv("REDIS_URL", "")
ES_URL = os.getenv("ES_URL", "")
KB_DIR = os.getenv("KB_DIR", "kb")

RERANKER_MODE = os.getenv("RERANKER_MODE", "base_plus_rules")  # frozen: no fine-tuning
