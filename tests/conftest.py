import os

# The tests use the default broker's costs, whatever the local agents/config.json says (agents reads it on import).
os.environ["OGJ_CORRETORA"] = "xtb"
