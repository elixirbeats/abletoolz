# Edit a plugin device in place within a format, rebuild it across formats

A plugin device is written two ways and never a third. A change that keeps the format, such as a new file to point at, a new name or id, or new preset bytes, edits those fields on the existing info element and touches nothing else, so every child Live wrote that the model does not know survives byte for byte. A change of format builds a new element from the model in the child order measured for that format and Live version, and swaps it in at the old element's position with its indentation.

Rebuilding is a keep-list: it drops every child it does not list. That is exactly right across formats, where the source format's fields have no counterpart, and exactly wrong within one, where it would silently strip fields nobody has measured. One writer that always rebuilt was the obvious shape and was rejected for that reason; the fidelity test in `test/test_plugin_device.py` is what holds the line.
