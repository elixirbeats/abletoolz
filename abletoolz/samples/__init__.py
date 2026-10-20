"""Finding samples on disk: the sample database and the matcher that picks a replacement.

``database`` indexes sample folders by name, size and modification time;
``matcher`` ranks the candidates it holds for a missing reference. Set
authoring and the plugin parsers both use them, which is why they sit beside
both rather than inside either.
"""
