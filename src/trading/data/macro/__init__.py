"""Macro collectors: Fed, BOJ, MOF, BLS, BEA (free tier).

Observations are stored point-in-time: a September revision of a July figure
is a new event with its own known_at, never an overwrite.
"""
