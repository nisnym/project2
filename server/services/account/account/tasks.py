"""Django Q2 task entrypoints.

Thin, and idempotent -- assume every task runs twice, because a worker
can die after the side effect but before the ack.
"""
