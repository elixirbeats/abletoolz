"""The files abletoolz reads and writes, as bytes in and typed values out.

One contract across every format here: ``parse``/``decode`` and
``encode`` are pure, and bad input raises a :class:`FormatError`. Reading a
path and writing one are thin wrappers on top, and a write is atomic. What a
format *means* to a set -- which clip, which device, which sample -- is the
business of the modules above this package, not of the codec.

* ``hex_text`` -- the hex text Live writes for binary data inside its XML.
* ``path_data`` -- the pre-11 ``FileRef/Data`` blob, a path in that hex text.
* ``live_document`` -- gzip around Live's XML: ``.als``, ``.alc``, ``.adv``,
  ``.adg``, ``.agr``.
* ``fxb`` -- the VST2 bank file, a chunk behind a 160 byte header, which is how
  a patch leaves or enters Live.
* ``asd`` -- Live's binary analysis file beside a sample: warp markers and
  analysis data.
"""

from abletoolz.formats.errors import FormatError

__all__ = ["FormatError"]
