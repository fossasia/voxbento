from fastapi.testclient import TestClient
from fastapi_app import app

client = TestClient(app)
response = client.post("/api/developer/clients/client_test_id_123/delete", data={"csrf_token": "test"})
print(response.status_code)
