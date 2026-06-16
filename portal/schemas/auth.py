from pydantic import BaseModel


class TokenRequest(BaseModel):
    token: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
