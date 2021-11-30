# -*- python -*-
from lsst.sconsUtils import scripts
from lsst.sconsUtils import state

import os

scripts.BasicSConstruct("butleruri", disableCc=True)
mypy = state.env.Command("mypy.log", "python/lsst/butlerUri",
                         "mypy python/lsst 2>&1 | tee -a mypy.log")
state.env.Alias("mypy", mypy)

# Propagate environment variables used only by this package through SCons.
envvars = ["BUTLERURI_TEST_TMP"]
for e in envvars:
    if e in os.environ:
        state.env["ENV"][e] = os.environ[e]
