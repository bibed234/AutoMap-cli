"""Malformed untrusted login/logout input must not raise server errors."""
from test_web_auth import auth_app


def test_unicode_logout_token_is_rejected(auth_app):
    client = auth_app().test_client()
    client.post('/login', data={'login': 'deltaaa', 'password': 'deltaaa00!'})
    assert client.post('/logout', data={'csrf_token': 'невірний-токен'}).status_code == 400
    assert client.get('/api/status').status_code == 200


def test_control_characters_in_next_do_not_crash_login(auth_app):
    client = auth_app().test_client()
    response = client.post('/login', data={
        'login': 'deltaaa', 'password': 'deltaaa00!', 'next': '/\t/[invalid-ipv6',
    })
    assert response.status_code == 302
    assert response.location == '/'
