"""
backend/app/models/common.py

Purpose:
    Shared Pydantic V2 model helpers, including BSON ObjectId validation and
    JSON serialization compatibility for Mongo-backed domain models.

Dependencies:
    - bson.ObjectId
    - pydantic.GetCoreSchemaHandler
    - pydantic_core.core_schema
"""

from typing import Any

from bson import ObjectId
from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema


class PyObjectId(str):
    """Pydantic V2 bridge type for BSON ObjectId."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.json_or_python_schema(
            json_schema=core_schema.str_schema(),
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(ObjectId),
                    core_schema.no_info_after_validator_function(
                        lambda x: ObjectId(x), core_schema.str_schema()
                    ),
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda x: str(x)
            ),
        )
