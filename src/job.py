from dataclasses import dataclass
from websockets import ClientConnection, ServerConnection
from enum import Enum

class JobStatus(Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    CANCELED = "CANCELED"
    FAILED = "FAILED"




@dataclass
class Job:
    status: JobStatus
    code: str
    conn: ServerConnection
    stdout: str
    stderr: str

    def __init__(self, code: str, conn: ServerConnection):
        self.status = JobStatus.PENDING
        self.code = code
        self.conn = conn
