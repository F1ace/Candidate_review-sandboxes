from pydantic import BaseModel
class PracticeCodeRequest(BaseModel):
    task_id: str
    language: str = "python"
    code: str

class PracticeSqlRequest(BaseModel):
    task_id: str
    sql_scenario_id: str
    query: str

