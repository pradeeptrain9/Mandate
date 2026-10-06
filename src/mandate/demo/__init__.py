"""Demo scenes, and the two attackers they need.

Kept inside the package rather than in `scripts/` so the scenes are importable and
testable. A demo that only exists as a shell script is a demo nobody can assert
anything about.
"""

from .scenes import SCENES, Scene, SceneContext

__all__ = ["SCENES", "Scene", "SceneContext"]
