"""Plugin state: the third axis of the plugin transform model, and the hard one.

Container and identity are finite knowledge. State is per plugin and per
vendor, and getting it wrong does not fail: the device loads and sounds wrong.
The package defines nothing itself; every codec raises the error in ``errors``.

* :mod:`~abletoolz.plugins.state.plugin_map` -- one plugin, and how its patch
  crosses between its own VST2, VST3 and AU formats. Each vendor's module
  (``fabfilter``, ``izotope``, ...) declares its maps; :mod:`~abletoolz.plugins.state.catalog`
  and :mod:`~abletoolz.plugins.state.all_plugins` find them by stored id, along
  with the converters from one plugin into another.
* :mod:`~abletoolz.plugins.state.measured` -- which rung each crossing sits on
  and how that was learned. Vendor-compat is a rung, not a transform: the bytes
  go across verbatim and the vendor's declaration is the evidence.
* :mod:`~abletoolz.plugins.state.framings`, :mod:`~abletoolz.plugins.state.fxbk`,
  :mod:`~abletoolz.plugins.state.derived` and :mod:`~abletoolz.plugins.state.audio_unit`
  -- the containers, one module per family rather than per vendor.
* :mod:`~abletoolz.plugins.state.model` -- fixed-offset byte maps and lossless edits.

What a VST3's ControllerState holds beside the patch is in
:mod:`abletoolz.plugins.container.controller`.
"""
