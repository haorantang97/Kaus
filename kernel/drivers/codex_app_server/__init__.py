"""Codex official ``codex app-server`` transport (JSON-RPC 2.0 over stdio).

This package is the one place in Kaus that knows the app-server protocol
(``codex-rs/app-server-protocol``, v2 methods). It is an *alternate transport*
for the Codex connection: the ACP preset keeps the third-party ``codex-acp``
adapter as the default, and the connection setting ``transport = "app-server"``
switches a connection to this driver (see ``AcpPreset.transports``).

Both transports talk to the same native thread store (``$CODEX_HOME/sessions``)
through the same ``codex app-server`` process; ``codex-acp`` returns the
app-server ``thread.id`` as its ACP ``sessionId``, so conversation ids are
shared between the two paths. Whether a live ``thread/resume`` of a thread that
``codex-acp`` created succeeds has not been observed on a real binary yet.
"""
