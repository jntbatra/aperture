"""Multi-tenancy: who is asking, and whose data they may reach.

Kept in its own package because the boundary it draws is worth being able to
see. Everything in ``sqlagent`` proper answers "what is the right SQL for this
question"; everything here answers "may this caller ask it at all, and against
what". Mixing the two is how a scoping check ends up somewhere it can be
forgotten.
"""
