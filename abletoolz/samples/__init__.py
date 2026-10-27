"""Finding samples on disk: the sample database and the lookup that picks a replacement.

``database`` indexes sample folders by name, size, date and Live's checksum,
into the sample database and, for files only a plugin loads, the plugin files
database beside it; ``relink`` asks it for the candidates for a missing reference and ranks the
ones the reference allows. Set authoring and plugin file relinking both use
them, which is why they sit beside both rather than inside either.
"""
