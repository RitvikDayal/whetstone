"""The `code-defects` lens: hunt, reproduce, falsify, grade.

The stages live in sibling modules and are wired together by the pack. This
package holds the deterministic half -- `grade.py` -- which is the layer that
decides, and which never consults what the model thought of itself.
"""
