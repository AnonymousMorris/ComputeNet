from dataclasses import dataclass, asdict
from enum import Enum
from typing import Union
import json
from job import Job

from websockets.http11 import d


class MessageType(str, Enum):
    REGISTER = "REGISTER"
    SUBMIT_CODE = "SUBMIT_CODE"
    SUCCESS = "SUCCESS"
    RESULT = "RESULT"

@dataclass
class BaseMessage:
    type: MessageType

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(data) -> "Message":
        obj = json.loads(data)
        msg_type = obj["type"]
        if msg_type == "SUBMIT_CODE":
            return SubmitCodeMessage.from_json(obj)
        elif msg_type == "REGISTER":
            return RegisterMessage.from_json(obj)
        else:
            raise ValueError(f"Unknown Message Type {msg_type}")

@dataclass
class RegisterMessage(BaseMessage):
    pass

@dataclass
class SubmitCodeMessage(BaseMessage):
    code: str

    @staticmethod
    def from_json(data) -> "SubmitCodeMessage":
        obj = json.loads(data)
        return SubmitCodeMessage(
            type = MessageType(obj["type"]),
            code = obj["code"]
        )

@dataclass
class ResultResponse(BaseMessage):
    stdout: str
    stderr: str

    @staticmethod
    def from_job(job: Job) -> "ResultResponse":
        return ResultResponse(
            type = MessageType(MessageType.RESULT),
            stdout = job.stdout,
            stderr = job.stderr
        )

    @staticmethod
    def from_json(data) -> "ResultResponse":
        obj = json.loads(data)
        return ResultResponse(
            type = MessageType(obj["type"]),
            stdout = obj["stdout"],
            stderr = obj["stderr"]
        )
    



Message = Union[RegisterMessage, SubmitCodeMessage, ResultResponse]

# @dataclass
# class NodeResponse:
#     type: MessageType
#     stdout: str
#     stderr: str
#
#     def to_json(self) -> str:
#         return json.dumps(asdict(self))
#
#     def from_json(self, data) -> "NodeResponse":
#         obj = json.loads(data)
#         return NodeResponse(
#             type = MessageType[obj["type"]],
#             stdout = obj["stdout"],
#             stderr = obj["stderr"]
#         )

# Message: Union[ClientMessage, NodeResponse]
