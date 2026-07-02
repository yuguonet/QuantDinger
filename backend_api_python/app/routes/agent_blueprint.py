# -*- coding: utf-8 -*-
"""
/api/agent/* —  Thin proxy routing to app.agent.flask_app.

All agent logic lives in app/agent/flask_app.py (or the llm/ subpackage).
This file just fixes sys.path so the agent modules can import each other,
then re-exports flask_app's working routes under the /api/agent prefix.
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Ensure app/agent/ is on sys.path so "from llm import ..." and other
# agent-relative imports work at runtime.
_agent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent"))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from flask import Blueprint

# Import flask_app's working blueprint, register it to a throwaway app so
# its view_functions dict is populated, then copy every route under /api/agent.
from app.agent.flask_app import agent_v2_bp as _src_bp
from flask import Flask as _Flask

_temp = _Flask(__name__)
_temp.register_blueprint(_src_bp)

agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

for _r in _temp.url_map.iter_rules():
    if not _r.endpoint.startswith("agent_v2."):
        continue
    rel_rule = _r.rule.replace("/api/agent-v2", "") or "/"
    methods = list(_r.methods - {"HEAD", "OPTIONS"})
    view_func = _temp.view_functions[_r.endpoint]
    short_ep = _r.endpoint.split(".", 1)[1]
    agent_bp.add_url_rule(rel_rule, endpoint=short_ep, view_func=view_func, methods=methods)

logger.info("Agent blueprint registered with %d routes", len(agent_bp.deferred_functions))
