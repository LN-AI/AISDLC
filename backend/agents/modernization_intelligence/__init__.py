"""Modernization Intelligence — whole-codebase conversion.

One agent, called once per source unit by `services/modernization/runner.py`. It is
deliberately small: the hard parts of modernization (ordering, resumability, coherence,
secret handling, validation) are deterministic and live in `services/modernization/`. The
model's job is the one thing only it can do — translate one file, given a fixed charter and
the public signatures of what its dependencies became.
"""
