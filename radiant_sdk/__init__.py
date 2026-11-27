"""RADiant miner SDK.

The same package is baked into every validator sandbox image, so the base
class, the harness and the static checks a miner runs locally are exactly the
ones the validator runs. ``SDK_VERSION`` is what a campaign manifest pins
(``solution.sdk_version``); it also fixes the base image contents.
"""

SDK_VERSION = "1.0.0"
SOLUTION_FORMAT = "r1"
