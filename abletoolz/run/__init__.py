"""What a run does to each set, and what it leaves behind: the record, the console lines, the sidecar.

``job`` runs one set: ``SetJob`` is what the command line asked of it and
``run_set`` does that, measuring each finding once. ``batch.run_sets`` runs one
job over many sets in worker processes, each set's failure kept to its own record. ``report`` is the record a
program reads, ``console`` renders the same facts for a person, and ``meta``
writes the ``.meta.yaml`` sidecar beside each set. ``run_set`` builds the record
and the sidecar from the same findings it printed, so the three cannot disagree
about what happened.
"""
