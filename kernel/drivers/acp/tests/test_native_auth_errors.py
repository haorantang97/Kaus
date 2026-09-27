from drivers.acp.failure_hints import looks_like_auth_error


def test_internal_error_requires_an_explicit_authentication_cause():
    assert looks_like_auth_error(-32603, 'Internal error: You need to sign in to use this model.')
    assert looks_like_auth_error(-32603, 'Internal error', {'message': 'Failed to authenticate: OAuth session expired and could not be refreshed'})
    assert not looks_like_auth_error(-32603, 'Internal error: auth metadata parser broke')
    assert not looks_like_auth_error(-32602, 'Authentication required')
