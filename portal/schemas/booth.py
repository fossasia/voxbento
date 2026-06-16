from pydantic import BaseModel


class CreateBoothRequest(BaseModel):
    language_code: str
    language: str = ""
    room_id: int | None = None
    instance: str = "primary"
