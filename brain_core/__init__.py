"""brain_core — consolidated in-process modules for the unified second-brain FastAPI.

Every module lives as a submodule of this package so `brain_server.py` can do
`from brain_core import search_unified, temporal, learn, ...` instead of
`sys.path.insert`ing into multiple legacy locations.
"""
