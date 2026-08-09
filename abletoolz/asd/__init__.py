"""abletoolz.asd  —  Ableton .asd binary file utilities.

Submodules:
  parser   — schema-driven .asd parser/serializer, warp-grid authoring (no heavy deps)
  writer   — write_grid(): rewrite or cold-synthesize a constant-tempo warp grid
  analysis — BPM detection and onset finding via librosa (optional dep)
"""

from abletoolz.asd.parser import AsdFile, WarpMarker
from abletoolz.asd.writer import synthesize_asd, write_grid

__all__ = ["AsdFile", "WarpMarker", "synthesize_asd", "write_grid"]
