"""
Deferred Phase 2 (per architecture plan — not implemented here):

- Hybrid retrieval: BM25 or keyword-heavy parallel query; Chroma has no native BM25 —
  consider pgvector + tsvector or a dedicated hybrid engine.
- Metadata `where` filters on Chroma from hypothesis expert_role / content_type.
- Background «cold path» job after sessions: episodic notes + pattern_store bullets
  merged into long_term_profile without blocking the hot request path.

See retrieval.py module docstring and multi_query_retrieve for the current v2 baseline.
"""
