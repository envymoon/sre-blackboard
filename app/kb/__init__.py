"""The knowledge base (M6, ADR-0010): runbooks and postmortems, chunked, embedded and searched.

    kb/*.md --ingest--> kb_documents, kb_chunks (tsvector + vector, per tenant)
    query --> BM25 top 20 + vector top 20 --> RRF (k=60) --> cross-encoder rerank --> threshold

The agents reach it through one read-only tool, ``kb_search`` (app/tools/kb.py). A runbook
says what usually goes wrong; only live data says what went wrong now, so a finding may
cite a knowledge chunk only next to live evidence (app/agents/citations.py).
"""
