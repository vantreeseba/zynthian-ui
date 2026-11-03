# -*- coding: utf-8 -*-
"""Test setup: `import gemifi` (the support package) and `zyngine.ctrldev.*` without
loading the rest of Zynthian.

zyngine/__init__.py imports every engine, so the real packages are replaced by bare ones that
point at the same directories; the Zynthian modules the driver imports are stubbed in
test_driver.py.
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
GEMIFI = os.path.dirname(HERE)
CTRLDEV = os.path.dirname(GEMIFI)
ZYNGINE = os.path.dirname(CTRLDEV)

sys.path.insert(0, CTRLDEV)     # `import gemifi`
sys.path.insert(0, HERE)        # `import fakes`

for name, path in (("zyngine", ZYNGINE), ("zyngine.ctrldev", CTRLDEV)):
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [path]
        sys.modules[name] = pkg
