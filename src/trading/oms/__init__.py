"""Order management system.

OMS owns broker order creation. Commands move through a strict state machine;
UNKNOWN commands are resolved only by reconciliation, never blind retry.
"""
