"""The grade vocabulary, shared by the spine and the lens layer.

Here rather than in `lenses/code_defects/grade.py` for the reason `Severity`
sits in `severity.py`: `lenses/base.py` validates `Candidate.grade`, and core
importing a lens pack to find out what a valid grade is puts the plugin layer
underneath the contract every plugin implements. `code_defects/grade.py` keeps
the grading FUNCTION, which is genuinely that lens's, and imports the letters
from here.

The letters are declared best-to-worst so callers can sort on them.
"""

from __future__ import annotations

from enum import StrEnum


class Grade(StrEnum):
    """What a finding's evidence is worth, decided by code.

    A -- reproduced, executable, survived falsification.
    B -- survived, but nothing runnable backs it.
    C -- not reproduced.
    D -- the falsifier killed it.

    A lens that does not grade -- `hygiene` measures a threshold and is done --
    produces candidates with no grade at all, which is not the same as D. D is
    a verdict; absent is the absence of one, and a finding must never be shown
    as killed because nobody looked.
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
