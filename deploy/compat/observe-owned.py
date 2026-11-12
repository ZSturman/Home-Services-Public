#!/usr/bin/env python3
"""Use this release's observer without deploying unrelated gateway changes."""
import importlib.util
import sys
from pathlib import Path

from home_services.cli import main

spec = importlib.util.spec_from_file_location("home_services.observe", Path(__file__).with_name("observe.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(main())
