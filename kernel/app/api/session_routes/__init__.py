"""Endpoint groups of the session API, assembled by :func:`~app.api.session_router.build_session_router`.

- :mod:`~app.api.session_routes.common`: error type, request bodies, route description, exception translation;
- :mod:`~app.api.session_routes.context`: shared dependencies and helpers;
- :mod:`~app.api.session_routes.conversations`: conversation records and native history;
- :mod:`~app.api.session_routes.settings`: model / reasoning / permission settings and catalog reads;
- :mod:`~app.api.session_routes.messages`: sending messages, attachments and files;
- :mod:`~app.api.session_routes.events`: SSE stream and snapshot;
- :mod:`~app.api.session_routes.interactions`: interrupt, interaction answers, stop;
- :mod:`~app.api.session_routes.surfaces`: Card ⇄ external terminal handoff;
- :mod:`~app.api.session_routes.capabilities`: Project capability writes;
- :mod:`~app.api.session_routes.debug`: forensic endpoints behind ``debug_endpoints``.

Each group exposes ``build_*_routes(ctx)`` returning :class:`~app.api.session_routes.common.RouteSpec`
rows; registration order and dependencies are decided by the router builder. The web framework is
imported inside those functions only, as in :mod:`app.api.router`.
"""
