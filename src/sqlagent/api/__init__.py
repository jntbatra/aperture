"""HTTP interface: a FastAPI application exposing the agent.

The application object is deliberately **not** re-exported here. Doing
``from sqlagent.api.app import app`` in this file binds the name ``app`` on the
package, which then shadows the ``sqlagent.api.app`` submodule — so
``import sqlagent.api.app`` yields the FastAPI instance rather than the module,
and patching anything inside it becomes impossible.

Import the application where it is needed:

    from sqlagent.api.app import app          # in code
    uvicorn sqlagent.api.app:app              # on the command line
"""
