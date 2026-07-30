"""Test setup for the api Lambda.

`handler.py` builds its boto3 resource and clients at import time, which is
the right thing for a Lambda (they get reused across warm invocations) but
means the module cannot be imported without boto3 present and credentials
resolvable. So boto3 is stubbed into sys.modules here, before pytest
collects anything, and the required environment variables are set.

Nothing in this suite touches AWS. It runs offline, in about a second, and
costs nothing -- which is the whole point, given that every other
verification in this project has been a manual Lambda invoke against real
infrastructure.
"""

import os
import sys
import types
from unittest.mock import MagicMock

# --- stub boto3 before handler.py imports it ------------------------------
_boto3 = types.ModuleType("boto3")
_boto3.resource = MagicMock()
_boto3.client = MagicMock()
sys.modules["boto3"] = _boto3

_conditions = types.ModuleType("boto3.dynamodb.conditions")


class Key:
    """Just enough of boto3's Key to record what a query asked for."""

    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return ("eq", self.name, value)


_conditions.Key = Key
sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
sys.modules["boto3.dynamodb.conditions"] = _conditions

os.environ.setdefault("WATCHES_TABLE", "watches")
os.environ.setdefault("WATCH_TARGETS_TABLE", "targets")
os.environ.setdefault("PLANNER_FUNCTION_ARN", "arn:aws:lambda:::function:planner")
os.environ.setdefault("CHECKER_FUNCTION_ARN", "arn:aws:lambda:::function:checker")
os.environ.setdefault("SCHEDULER_ROLE_ARN", "arn:aws:iam:::role/sched")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
