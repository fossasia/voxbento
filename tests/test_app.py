from app import app


def test_healthz_returns_ok():
    client = app.test_client()

    response = client.get('/healthz')

    assert response.status_code == 200
    assert response.get_json()['ok'] is True


def test_home_redirects_to_demo_booth():
    client = app.test_client()

    response = client.get('/')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/interpreter/demo-booth')
